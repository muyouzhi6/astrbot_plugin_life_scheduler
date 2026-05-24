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
    _EMPTY_COMPLETION_RETRIES = 1

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
        self,
        date: datetime.datetime | None = None,
        umo: str | None = None,
        extra: str | None = None,
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
            manual_extra = self._normalize_extra(extra)
            prompt = self._build_prompt(ctx, manual_extra)
            sid_base = f"life_scheduler_gen_{date_str}"
            content = await self._call_llm(prompt, sid=f"{sid_base}_0")

            payload = self._extract_json_obj(content)
            enforce_style = not manual_extra
            ok, reason = self._validate_payload(
                payload,
                ctx,
                enforce_style=enforce_style,
                manual_extra=manual_extra,
            )
            for attempt in range(1, self._STYLE_ENFORCE_RETRIES + 1):
                if ok:
                    break
                if manual_extra:
                    repair_prompt = self._build_manual_repair_prompt(
                        ctx, content, reason, manual_extra
                    )
                else:
                    repair_prompt = self._build_style_repair_prompt(ctx, content, reason)
                content = await self._call_llm(repair_prompt, sid=f"{sid_base}_{attempt}")
                payload = self._extract_json_obj(content)
                ok, reason = self._validate_payload(
                    payload,
                    ctx,
                    enforce_style=enforce_style,
                    manual_extra=manual_extra,
                )

            if not ok or not payload:
                raise ValueError(f"模型未遵循生成约束：{reason}")

            data = self._to_schedule_data(
                payload, date_str, ctx, manual_extra=manual_extra
            )
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
    @staticmethod
    def _normalize_extra(extra: str | None) -> str:
        return str(extra or "").strip()

    @staticmethod
    def _normalize_requirement_text(text: str) -> str:
        return re.sub(r"[\s\"'“”‘’`，,。.!！?？:：;；、（）()\[\]【】<>《》]", "", text)

    _NEGATIVE_MARKER_RE = re.compile(r"不要再|不要|不能|不许|不想|不用|不需要|别再|别|避免|禁止|拒绝")
    _OUTFIT_TERM_RE = re.compile(
        r"吊带裙|吊带衫|连衣裙|半身裙|牛仔裤|黑丝|白丝|丝袜|吊带|短裙|长裙|裙|裤|袜|鞋|靴|衣|衫|外套|内衣|内裤|帽|包|耳钉|项链|手链|口红|妆|黑色|白色|红色|粉色|蓝色|绿色|黄色|紫色|灰色|米色|棕色|金色|银色|风格|穿搭"
    )
    _SCHEDULE_TERM_RE = re.compile(
        r"下午茶|咖啡店|奶茶店|电影院|吃饭|睡觉|看书|出门|上班|上课|约会|咖啡|奶茶|电影|逛街|散步|阅读|学习|工作|健身|运动|睡|洗澡|拍照|做饭|烘焙|画画|游戏|瑜伽|公园|学校|公司|商场|餐厅|居酒屋|便利店|吃|喝"
    )
    _NEGATED_TERM_PREFIX_RE = re.compile(
        r"(?:不要再|不用再|不需要再|不想再|不能再|不许再|别再)$|"
        r"不(?:再|要|想|用|需要|许|去|穿|戴|安排|进行|做)?$|"
        r"别(?:再|去|穿|戴|安排|进行|做)?$|"
        r"避免$|禁止$|拒绝$|无$|没有$"
    )

    @classmethod
    def _strip_manual_term(cls, text: str) -> str:
        item = cls._normalize_requirement_text(text)
        item = cls._NEGATIVE_MARKER_RE.sub("", item)
        item = re.sub(
            r"^(?:今天|今日|今儿|这次|请|麻烦|帮我|给我|让她|要|想|希望|必须|一定要|特别|注意|日程|安排|一个|一场|一份|一下|穿搭|穿着|穿|戴|换上|搭配|去|到|在|做|进行|来|搞)+",
            "",
            item,
        )
        item = re.sub(r"(?:一点|一些|一下|日程|安排)$", "", item)
        return item

    @classmethod
    def _append_requirement(
        cls, requirements: dict[str, list[str]], bucket: str, item: str
    ) -> None:
        if len(item) >= 2 and item not in requirements[bucket]:
            requirements[bucket].append(item)

    @classmethod
    def _extract_known_terms(cls, item: str, term_re: re.Pattern) -> list[str]:
        terms: list[str] = []
        for match in term_re.finditer(item):
            term = match.group(0)
            if term not in terms:
                terms.append(term)
        return terms

    @classmethod
    def _append_forbidden_requirement(
        cls, requirements: dict[str, list[str]], item: str
    ) -> None:
        terms = cls._extract_known_terms(item, cls._OUTFIT_TERM_RE)
        for term in cls._extract_known_terms(item, cls._SCHEDULE_TERM_RE):
            if term not in terms:
                terms.append(term)
        if terms:
            for term in terms:
                cls._append_requirement(requirements, "forbidden", term)
            return
        cls._append_requirement(requirements, "forbidden", item)

    @classmethod
    def _append_positive_requirement(
        cls, requirements: dict[str, list[str]], item: str
    ) -> None:
        outfit_terms = cls._extract_known_terms(item, cls._OUTFIT_TERM_RE)
        schedule_terms = cls._extract_known_terms(item, cls._SCHEDULE_TERM_RE)

        for term in outfit_terms:
            cls._append_requirement(requirements, "required_outfit", term)
        for term in schedule_terms:
            cls._append_requirement(requirements, "required_schedule", term)

        if not outfit_terms and not schedule_terms:
            cls._append_requirement(requirements, "required_any", item)

    @classmethod
    def _extract_manual_requirements(cls, extra: str) -> dict[str, list[str]]:
        requirements = {
            "required_outfit": [],
            "required_schedule": [],
            "required_any": [],
            "forbidden": [],
        }
        segments = re.split(r"[，,。.!！?？:：;；、\n]", extra)
        for segment in segments:
            if not segment:
                continue
            is_negative = bool(cls._NEGATIVE_MARKER_RE.search(segment))
            parts = re.split(r"和|与|以及|并且|然后|再", segment)
            for part in parts:
                item = cls._strip_manual_term(part)
                if len(item) < 2:
                    continue
                if is_negative:
                    cls._append_forbidden_requirement(requirements, item)
                else:
                    cls._append_positive_requirement(requirements, item)
        for key, value in requirements.items():
            requirements[key] = value[:8]
        return requirements

    def _build_prompt(self, ctx: ScheduleContext, extra: str | None = None) -> str:
        extra = self._normalize_extra(extra)
        ctx_dict = asdict(ctx)  # 实际有的字段
        if extra:
            ctx_dict["outfit_style"] = "用户指定"
            ctx_dict["schedule_type"] = "用户指定"

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

        if extra:
            prompt += (
                "\n\n## ✅ 用户补充强制约束（最高优先级，必须严格遵循）\n"
                f"- 用户补充要求：{extra}\n"
                "- 用户补充要求优先级高于今日主题、心情色彩、穿搭风格、日程类型和历史日程参考。\n"
                "- 如果用户补充要求与上文随机创意池或模板中的穿搭风格冲突，必须以用户补充要求为准。\n"
                "- 不得忽略、替换、弱化或用随机创意池覆盖用户补充要求中的具体衣物、场景和活动。\n"
                "- 你必须只输出 JSON 对象本体（不要 Markdown/代码块/解释）。\n"
                '- JSON 必须包含字段 "outfit_style"、"outfit"、"schedule"。\n'
                '- 当用户指定了具体穿搭时，"outfit" 必须直接包含这些具体穿搭元素。\n'
            )
        elif ctx.outfit_style:
            prompt += (
                "\n\n## ✅ 强制约束（必须严格遵循）\n"
                f"- 你必须严格遵循穿搭风格：【{ctx.outfit_style}】（不得替换/混用其他风格）。\n"
                "- 你必须只输出 JSON 对象本体（不要 Markdown/代码块/解释）。\n"
                f"- JSON 必须包含字段 \"outfit_style\"，且其值必须严格等于 \"{ctx.outfit_style}\"。\n"
                f"- 字段 \"outfit\" 的第一行必须以 \"风格：{ctx.outfit_style}\" 开头。\n"
            )

        return prompt

    async def _call_llm(self, prompt: str, *, sid: str = "life_scheduler_gen") -> str:
        provider_id = self.config.get("llm_provider")
        provider = (
            self.context.get_provider_by_id(provider_id) if provider_id else None
        ) or self.context.get_using_provider()

        if not provider:
            raise RuntimeError("No provider")

        try:
            for attempt in range(self._EMPTY_COMPLETION_RETRIES + 1):
                resp = await provider.text_chat(prompt, session_id=sid)
                text = self._extract_completion_text(resp)
                if text:
                    return text
                if attempt < self._EMPTY_COMPLETION_RETRIES:
                    logger.warning("LLM completion 为空，准备重试一次")
            raise RuntimeError("API返回的completion为空")
        finally:
            await self._cleanup_session(sid)

    @staticmethod
    def _extract_completion_text(resp: object) -> str:
        if resp is None:
            return ""
        for key in ("completion_text", "completion", "text", "content"):
            value = getattr(resp, key, None)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        return ""

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

    def _validate_payload(
        self,
        payload: dict | None,
        ctx: ScheduleContext,
        *,
        enforce_style: bool = True,
        manual_extra: str = "",
    ) -> tuple[bool, str]:
        if not payload:
            return False, "未能解析出 JSON 对象"

        outfit = str(payload.get("outfit", "")).strip()
        schedule = str(payload.get("schedule", "")).strip()
        if not outfit:
            return False, "outfit 不能为空"
        if not schedule:
            return False, "schedule 不能为空"

        requirement_errors = self._manual_requirement_errors(payload, manual_extra)
        if requirement_errors:
            return False, "用户补充要求未满足：" + "；".join(requirement_errors)

        required = (ctx.outfit_style or "").strip()
        if not enforce_style:
            return True, ""
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

    def _manual_requirement_errors(self, payload: dict, manual_extra: str) -> list[str]:
        manual_extra = self._normalize_extra(manual_extra)
        if not manual_extra:
            return []

        requirements = self._extract_manual_requirements(manual_extra)
        if not any(requirements.values()):
            return []

        outfit = self._normalize_requirement_text(str(payload.get("outfit", "")))
        schedule = self._normalize_requirement_text(str(payload.get("schedule", "")))
        any_text = f"{outfit}{schedule}"
        errors: list[str] = []

        missing_outfit = [
            term for term in requirements["required_outfit"] if term not in outfit
        ]
        if missing_outfit:
            errors.append("穿搭缺少 " + ", ".join(missing_outfit))

        missing_schedule = [
            term for term in requirements["required_schedule"] if term not in schedule
        ]
        if missing_schedule:
            errors.append("日程缺少 " + ", ".join(missing_schedule))

        missing_any = [term for term in requirements["required_any"] if term not in any_text]
        if missing_any:
            errors.append("内容缺少 " + ", ".join(missing_any))

        forbidden_hits = [
            term
            for term in requirements["forbidden"]
            if self._has_unnegated_term(any_text, term)
        ]
        if forbidden_hits:
            errors.append("出现了用户要求避免的内容 " + ", ".join(forbidden_hits))

        return errors

    @classmethod
    def _has_unnegated_term(cls, text: str, term: str) -> bool:
        if term not in text:
            return False
        for match in re.finditer(re.escape(term), text):
            prefix = text[max(0, match.start() - 6) : match.start()]
            if cls._NEGATED_TERM_PREFIX_RE.search(prefix):
                continue
            return True
        return False

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

    def _build_manual_repair_prompt(
        self, ctx: ScheduleContext, bad_text: str, reason: str, extra: str
    ) -> str:
        return (
            "你之前的输出未通过校验，需要按用户补充要求重写。\n"
            f"校验原因：{reason}\n"
            f"日期：{ctx.date_str} {ctx.weekday} {ctx.holiday}\n"
            f"用户补充要求（最高优先级）：{extra}\n\n"
            "必须遵循：\n"
            "- 用户补充要求高于随机创意池、穿搭风格、日程类型和历史日程。\n"
            "- 不得忽略、替换或弱化用户指定的具体穿搭、场景和活动。\n"
            "- 请只输出 JSON 对象本体，不要 Markdown，不要解释。\n"
            '- 输出 JSON 必须包含字段：outfit_style、outfit、schedule。\n\n'
            "你之前的输出（供参考，可能不合规）：\n"
            f"{bad_text}\n"
        )

    def _to_schedule_data(
        self,
        payload: dict,
        date_str: str,
        ctx: ScheduleContext,
        *,
        manual_extra: str = "",
    ) -> ScheduleData:
        outfit = str(payload.get("outfit", "")).strip() or "日常休闲装"
        schedule = str(payload.get("schedule", "")).strip() or "无"
        if manual_extra:
            outfit_style = "用户指定"
        else:
            outfit_style = str(payload.get("outfit_style", "")).strip() or (
                ctx.outfit_style or ""
            )
        return ScheduleData(
            date=date_str,
            outfit_style=outfit_style,
            outfit=outfit,
            schedule=schedule,
        )
