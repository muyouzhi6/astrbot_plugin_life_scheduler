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
        self, date: datetime.datetime | None = None, umo: str | None = None
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
            prompt = self._build_prompt(ctx)
            content = await self._call_llm(prompt)
            data = self._parse_result(content, date_str)
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
            **self._pick_diversity(),
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

    def _pick_diversity(self) -> dict:
        pool = self.config["pool"]
        return {
            "daily_theme": random.choice(pool["daily_themes"]),
            "mood_color": random.choice(pool["mood_colors"]),
            "outfit_style": random.choice(pool["outfit_styles"]),
            "schedule_type": random.choice(pool["schedule_types"]),
        }

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

            items.append(
                f"[{date.strftime('%Y-%m-%d')}] 穿搭：{outfit} 日程：{schedule}"
            )

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

    def _build_prompt(self, ctx: ScheduleContext) -> str:
        return self.config["prompt_template"].format(**asdict(ctx))

    async def _call_llm(self, prompt: str) -> str:
        provider = self.context.get_using_provider()
        if not provider:
            raise RuntimeError("No provider")

        sid = "life_scheduler_gen"
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
    def _parse_result(self, text: str, date_str: str) -> ScheduleData:
        text = text.strip()
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

        start = text.find("{")
        if start == -1:
            return ScheduleData(date=date_str, outfit="日常休闲装", schedule="无")

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
                            return ScheduleData(
                                date=date_str,
                                outfit=data.get("outfit", "日常休闲装"),
                                schedule=data.get("schedule", "无"),
                            )
                        except Exception:
                            break

        return ScheduleData(
            date=date_str,
            outfit="日常休闲装",
            schedule=text,
        )
