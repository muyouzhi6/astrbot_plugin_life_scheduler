import asyncio
import datetime
import json
import random
import re
from dataclasses import asdict, dataclass

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

from .data import ScheduleData, ScheduleDataManager

_STYLE_PREFIX_RE = re.compile(
    r"^\s*(?:【?风格】?|\[?风格\]?)\s*[:：]\s*(?P<style>.+?)(?:\n|$)"
)


@dataclass(slots=True)
class ScheduleContext:
    date_str: str
    weekday: str
    holiday: str
    persona_desc: str
    history_schedules: str
    recent_chats: str
    daily_theme: str
    mood_color: str
    outfit_style: str
    schedule_type: str


class SchedulerGenerator:
    _STYLE_ENFORCE_RETRIES = 2

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        data_mgr: ScheduleDataManager,
    ):
        self.context = context
        self.config = config
        self.data_mgr = data_mgr

        self._gen_lock = asyncio.Lock()
        self._generating = False

    async def generate_schedule(
        self, date: datetime.datetime | None = None, umo: str | None = None, extra: str | None = None
    ) -> ScheduleData:
        async with self._gen_lock:
            if self._generating:
                raise RuntimeError("schedule_generating")
            self._generating = True

        data: ScheduleData | None = None
        date = date or datetime.datetime.now()
        date_str = date.strftime("%Y-%m-%d")
        try:
            logger.info(f"正在生成 {date_str} 的日程...")
            ctx = await self._collect_context(date, umo)
            prompt = self._build_prompt(ctx, extra)
            sid_base = f"life_scheduler_gen_{date_str}"
            content = await self._call_llm(prompt, sid=f"{sid_base}_0")

            payload = self._extract_json_obj(content)
            ok, reason = self._validate_payload(payload, ctx)
            for attempt in range(1, self._STYLE_ENFORCE_RETRIES + 1):
                if ok:
                    break
                repair_prompt = self._build_style_repair_prompt(ctx, content, reason)
                content = await self._call_llm(repair_prompt, sid=f"{sid_base}_{attempt}")
                payload = self._extract_json_obj(content)
                ok, reason = self._validate_payload(payload, ctx)

            if not ok or not payload:
                raise ValueError(f"模型未遵循穿搭风格约束：{reason}")

            data = self._to_schedule_data(payload, date_str, ctx)
            self.data_mgr.set(data)
            logger.info(
                f"日程生成成功: {json.dumps(asdict(data), ensure_ascii=False, indent=2)}"
            )
            return data
        except Exception as e:
            logger.error(f"日程生成失败: {e}")
            return ScheduleData(
                date=date_str, outfit="生成失败", schedule="生成失败", status="failed"
            )
        finally:
            async with self._gen_lock:
                self._generating = False
            if data:
                self.data_mgr.set(data)

    # ---------- context ----------

    async def _collect_context(
        self, data: datetime.datetime, umo: str | None
    ) -> ScheduleContext:
        return ScheduleContext(
            date_str=data.strftime("%Y年%m月%d日"),
            weekday=self._weekday(data),
            holiday=self._get_holiday_info(data.date()),
            persona_desc=await self._get_persona(),
            history_schedules=self._get_history(data),
            recent_chats=await self._get_recent_chats(umo),
            **self._pick_diversity(data.date()),
        )

    def _weekday(self, data):
        return ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][
            data.weekday()
        ]

    def _get_holiday_info(self, date: datetime.date) -> str:
        """获取节日信息（中国）"""
        try:
            import holidays

            cn_holidays = holidays.CN()
            holiday_name = cn_holidays.get(date)
            if holiday_name:
                return f"今天是 {holiday_name}"
        except Exception:
            return ""
        return ""

    def _pick_diversity(self, today: datetime.date) -> dict:
        pool = self.config["pool"]
        return {
            "daily_theme": random.choice(pool["daily_themes"]),
            "mood_color": random.choice(pool["mood_colors"]),
            "outfit_style": self._pick_outfit_style(pool["outfit_styles"], today),
            "schedule_type": random.choice(pool["schedule_types"]),
        }

    def _pick_outfit_style(self, styles: list[str], today: datetime.date) -> str:
        styles = list(styles or [])
        if not styles:
            return ""

        lookback_days = int(self.config.get("reference_history_days", 0) or 0)
        if lookback_days <= 0 or len(styles) <= 1:
            return random.choice(styles)

        used: set[str] = set()
        for i in range(1, lookback_days + 1):
            date = today - datetime.timedelta(days=i)
            data = self.data_mgr.get(date)
            if not data or data.status != "ok":
                continue

            style = (getattr(data, "outfit_style", "") or "").strip()
            if not style:
                style = self._extract_style_from_outfit(data.outfit)
            if style:
                used.add(style)

        candidates = [s for s in styles if s not in used]
        return random.choice(candidates or styles)

    def _extract_style_from_outfit(self, outfit: str) -> str:
        if not outfit:
            return ""
        m = _STYLE_PREFIX_RE.match(outfit.strip())
        if not m:
            return ""
        return (m.group("style") or "").strip()

    def _get_history(self, today: datetime.date) -> str:
        items: list[str] = []

        days = self.config.get("reference_history_days", 0)
        if days <= 0:
            return "（无历史记录）"

        for i in range(1, days + 1):
            date = today - datetime.timedelta(days=i)
            data = self.data_mgr.get(date)
            if not data or data.status != "ok":
                continue

            outfit = data.outfit[:40]
            schedule = data.schedule[:60]
            style = (getattr(data, "outfit_style", "") or "").strip() or self._extract_style_from_outfit(data.outfit)

            if style:
                items.append(f"[{date.strftime('%Y-%m-%d')}] 风格：{style} 穿搭：{outfit} 日程：{schedule}")
            else:
                items.append(f"[{date.strftime('%Y-%m-%d')}] 穿搭：{outfit} 日程：{schedule}")

        return "\n".join(items) if items else "（无历史记录）"

    async def _get_recent_chats(
        self, umo: str | None = None, count: int | None = None
    ) -> str:
        """获取指定会话的最近聊天记录"""
        count = count or self.config["reference_recent_count"]

        if not umo or not count:
            return "无近期对话"

        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return "无最近对话记录"

            conv = await self.context.conversation_manager.get_conversation(umo, cid)
            if not conv or not conv.history:
                return "无最近对话记录"

            history = json.loads(conv.history)

            recent = history[-count:] if count > 0 else []

            formatted = []
            for msg in recent:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "user":
                    formatted.append(f"用户: {content}")
                elif role == "assistant":
                    formatted.append(f"我: {content}")

            return "\n".join(formatted)

        except Exception as e:
            logger.error(f"Failed to get recent chats for {umo}: {e}")
            return "获取对话记录失败"

    async def _get_persona(self) -> str:
        try:
            p = await self.context.persona_manager.get_default_persona_v3()
            return p.get("prompt") if isinstance(p, dict) else getattr(p, "prompt", "")
        except Exception:
            return "你是一个热爱生活、情感细腻的AI伙伴。"

    # ---------- llm ----------
    def _build_prompt(self, ctx: ScheduleContext, extra: str | None = None) -> str:
        ctx_dict = asdict(ctx)  # 实际有的字段
        tmpl_vars = set(re.findall(r"\{(\w+)\}", self.config["prompt_template"]))
        missing = tmpl_vars - ctx_dict.keys()
        if missing:
            logger.warning(
                f"prompt 模板存在 ScheduleContext 未提供的字段：{missing}| 已自动替换成空串"
            )

        # 统一补空值，避免 KeyError
        for k in missing:
            ctx_dict[k] = ""
        prompt = self.config["prompt_template"].format(**ctx_dict)

        if ctx.outfit_style:
            prompt += (
                "\n\n## ✅ 强制约束（必须严格遵循）\n"
                f"- 你必须严格遵循穿搭风格：【{ctx.outfit_style}】（不得替换/混用其他风格）。\n"
                "- 你必须只输出 JSON 对象本体（不要 Markdown/代码块/解释）。\n"
                f"- JSON 必须包含字段 \"outfit_style\"，且其值必须严格等于 \"{ctx.outfit_style}\"。\n"
                f"- 字段 \"outfit\" 的第一行必须以 \"风格：{ctx.outfit_style}\" 开头。\n"
            )

        # 如果有用户补充要求，追加到 prompt 末尾
        if extra:
            prompt += f"\n\n【用户补充要求】\n请在生成日程时特别注意以下要求：{extra}"

        return prompt

    async def _call_llm(self, prompt: str, *, sid: str = "life_scheduler_gen") -> str:
        provider_id = self.config.get("llm_provider")
        provider = (
            self.context.get_provider_by_id(provider_id) if provider_id else None
        ) or self.context.get_using_provider()

        if not provider:
            raise RuntimeError("No provider")

        try:
            resp = await provider.text_chat(prompt, session_id=sid)
            return resp.completion_text
        finally:
            await self._cleanup_session(sid)

    async def _cleanup_session(self, sid: str):
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(sid)
            if cid:
                await self.context.conversation_manager.delete_conversation(sid, cid)
        except Exception:
            pass

    # ---------- parse ----------
    def _extract_json_obj(self, text: str) -> dict | None:
        text = text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

        start = text.find("{")
        if start == -1:
            return None

        brace = 0
        in_string = False
        escape = False

        for i, ch in enumerate(text[start:], start=start):
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                    if brace == 0:
                        json_str = text[start : i + 1]
                        try:
                            data = json.loads(json_str)
                            return data if isinstance(data, dict) else None
                        except Exception:
                            return None

        return None

    def _validate_payload(self, payload: dict | None, ctx: ScheduleContext) -> tuple[bool, str]:
        if not payload:
            return False, "未能解析出 JSON 对象"

        outfit = str(payload.get("outfit", "")).strip()
        schedule = str(payload.get("schedule", "")).strip()
        if not outfit:
            return False, "outfit 不能为空"
        if not schedule:
            return False, "schedule 不能为空"

        required = (ctx.outfit_style or "").strip()
        if not required:
            return True, ""

        model_style = str(payload.get("outfit_style", "")).strip()
        if model_style != required:
            return False, f"outfit_style 必须严格等于 \"{required}\""

        if not re.match(
            rf"^\s*(?:风格|【风格】|\[风格\])\s*[:：]\s*{re.escape(required)}(?:\s|$)",
            outfit,
        ):
            return False, f"outfit 第一行必须以 \"风格：{required}\" 开头"

        return True, ""

    def _build_style_repair_prompt(self, ctx: ScheduleContext, bad_text: str, reason: str) -> str:
        required = (ctx.outfit_style or "").strip()
        return (
            "你之前的输出未通过校验，需要按要求重写。\n"
            f"校验原因：{reason}\n"
            f"必须使用穿搭风格：{required}\n\n"
            "请只输出 JSON 对象本体，不要 Markdown，不要解释。\n"
            "输出 JSON 必须包含字段：outfit_style、outfit、schedule。\n"
            f"其中 outfit_style 必须严格等于 \"{required}\"；outfit 第一行必须以 \"风格：{required}\" 开头。\n\n"
            "你之前的输出（供参考，可能不合规）：\n"
            f"{bad_text}\n"
        )

    def _to_schedule_data(self, payload: dict, date_str: str, ctx: ScheduleContext) -> ScheduleData:
        outfit = str(payload.get("outfit", "")).strip() or "日常休闲装"
        schedule = str(payload.get("schedule", "")).strip() or "无"
        outfit_style = str(payload.get("outfit_style", "")).strip() or (ctx.outfit_style or "")
        return ScheduleData(
            date=date_str,
            outfit_style=outfit_style,
            outfit=outfit,
            schedule=schedule,
        )
