import re

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images

from .core.data import ScheduleDataManager, WardrobeDataManager
from .core.generator import SchedulerGenerator
from .core.schedule import LifeScheduler
from .core.utils import build_character_state_injection, resolve_business_now


class LifeSchedulerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.schedule_data_file = self.data_dir / "schedule_data.json"
        self.wardrobe_data_file = self.data_dir / "wardrobe.json"

    async def initialize(self):
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.wardrobe_mgr = WardrobeDataManager(self.wardrobe_data_file)
        self.generator = SchedulerGenerator(
            self.context,
            self.config,
            self.data_mgr,
            self.wardrobe_mgr,
        )
        self.scheduler = LifeScheduler(
            context=self.context,
            config=self.config,
            task=self.generator.generate_schedule,
        )
        self.scheduler.start()

    def _save_config(self):
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    @staticmethod
    def _format_reference_umo(umo: str, *, truncate: bool = True) -> str:
        umo = str(umo or "").strip()
        if not umo:
            return "未配置"
        if not truncate or len(umo) <= 20:
            return umo
        return f"{umo[:8]}...{umo[-4:]}（共{len(umo)}字符）"

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.stop()

    async def _collect_image_paths(self, event: AstrMessageEvent) -> list[str]:
        """Collect direct and quoted images as provider-readable references.

        Args:
            event: Current AstrBot message event.

        Returns:
            Deduplicated local paths or URLs for at most four images.
        """
        components = getattr(getattr(event, "message_obj", None), "message", []) or []
        image_refs: list[str] = []

        for component in components:
            if isinstance(component, Image):
                try:
                    image_refs.append(await component.convert_to_file_path())
                except Exception as exc:
                    logger.warning("Failed to resolve direct image attachment: %s", exc)
                continue

            if not isinstance(component, Reply):
                continue

            embedded_image = False
            for quoted_component in component.chain or []:
                if not isinstance(quoted_component, Image):
                    continue
                embedded_image = True
                try:
                    image_refs.append(await quoted_component.convert_to_file_path())
                except Exception as exc:
                    logger.warning("Failed to resolve embedded quoted image: %s", exc)

            if embedded_image:
                continue

            try:
                image_refs.extend(await extract_quoted_message_images(event, component))
            except Exception as exc:
                logger.warning("Failed to resolve quoted image fallback: %s", exc)

        deduped: list[str] = []
        seen: set[str] = set()
        for image_ref in image_refs:
            image_ref = str(image_ref or "").strip()
            if image_ref and image_ref not in seen:
                seen.add(image_ref)
                deduped.append(image_ref)
        return deduped[:4]

    async def get_life_context(self, *, allow_generate: bool = True) -> dict:
        """Return today's cached life context for other plugins.

        Args:
            allow_generate: Whether a missing schedule may be generated through
                the LLM. Set this to ``False`` for read-only integrations.

        Returns:
            A DailySharing-compatible context dictionary, or an empty dictionary
            when today's schedule is unavailable.
        """
        today = resolve_business_now(self.config.get("schedule_time"))
        data = self.data_mgr.get(today)

        if not data and allow_generate:
            try:
                data = await self.generator.generate_schedule(today, None)
            except RuntimeError:
                return {}

        if not data or data.status == "failed":
            return {}

        # 构建返回格式（与 DailySharing 的 _parse_life_data 兼容）
        return {
            "outfit": data.outfit,
            "schedule": data.schedule,
            "meta": {
                "style": data.outfit_style,
            },
            "timeline": [],  # 如果有 timeline 数据可以在这里添加
        }

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入"""
        business_now = resolve_business_now(self.config.get("schedule_time"))
        today = business_now
        umo = event.unified_msg_origin
        data = self.data_mgr.get(today)
        if not data:
            try:
                data = await self.generator.generate_schedule(today, umo)
            except RuntimeError:
                return
        if data.status == "failed":
            return

        inject_text = build_character_state_injection(
            data.outfit,
            data.schedule,
            business_now=business_now,
        )

        wardrobe_text = self.wardrobe_mgr.for_prompt(limit=20, max_chars=8000)
        wardrobe_instructions = (
            "\n<life_wardrobe>\n"
            "这是插件保存的备选穿搭方案，仅在用户谈到衣柜或某套穿搭时使用。\n"
            f"{wardrobe_text}\n"
            "当用户要查看、删除或修改某套方案时，先调用 life_wardrobe_list 获取最新编号和完整描述，"
            "再调用对应的衣柜工具；用户明确要求加入衣柜时调用 life_wardrobe_add。\n"
            "当用户要求修改今天的穿搭或日程时调用 life_schedule_edit。除非用户明确提出，不要主动改写或删除数据。\n"
            "</life_wardrobe>\n"
        )

        req.system_prompt = (
            (req.system_prompt or "") + inject_text + wardrobe_instructions
        )
        logger.debug(f"[LLM] 添加的内在状态注入：{inject_text}")

    @filter.command("查看日程", alias={"life show"})
    async def life_show(self, event: AstrMessageEvent):
        """查看今日的日程"""
        today = resolve_business_now(self.config.get("schedule_time"))
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin

        data = self.data_mgr.get(today)
        if not data:
            try:
                yield event.plain_result("今日还没日程，正在生成...")
                data = await self.generator.generate_schedule(today, umo)
            except RuntimeError:
                yield event.plain_result("日程正在生成中，请稍后再查看")
                return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：\n{data.schedule}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重写日程", alias={"life renew"})
    async def life_renew(self, event: AstrMessageEvent, extra: GreedyStr = GreedyStr):
        """重写今日的日程，可附加文字或图片要求。"""
        today = resolve_business_now(self.config.get("schedule_time"))
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin
        image_paths = await self._collect_image_paths(event)
        extra = str(extra or "").strip()
        outfit_match = re.fullmatch(r"穿搭\s*(\d+)(?:\s*[，,。]\s*(.*))?", extra)
        if outfit_match and not image_paths:
            outfit_number = int(outfit_match.group(1))
            wardrobe_entry = self.wardrobe_mgr.find_by_number(outfit_number)
            if wardrobe_entry is None:
                yield event.plain_result(
                    f"没有找到穿搭{outfit_number}，请先发送“查看衣柜”确认编号。"
                )
                return
            extra_suffix = str(outfit_match.group(2) or "").strip()
            extra = f"严格采用衣柜穿搭{outfit_number}：{wardrobe_entry['description']}"
            if extra_suffix:
                extra += f"；{extra_suffix}"
            yield event.plain_result(f"正在按衣柜穿搭{outfit_number}重写今日日程...")
        elif extra and image_paths:
            yield event.plain_result("正在读取图片中的穿搭，并据此重写今日日程...")
        elif extra:
            yield event.plain_result(f"正在根据补充要求重写今日日程：{extra}")
        elif image_paths:
            yield event.plain_result("正在读取图片中的穿搭，并重写今日日程...")
        else:
            yield event.plain_result("正在重写今日日程...")
        try:
            data = await self.generator.generate_schedule(
                today,
                umo,
                extra=extra,
                image_paths=image_paths,
            )
        except RuntimeError:
            yield event.plain_result("已有日程生成任务在进行中，请稍后再试")
            return
        yield event.plain_result(
            f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：{data.schedule}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("放进衣柜", alias={"life wardrobe add"})
    async def wardrobe_add(self, event: AstrMessageEvent, note: GreedyStr = GreedyStr):
        """将文字或图片整理成穿搭方案并放进衣柜。"""
        note = str(note or "").strip()
        image_paths = await self._collect_image_paths(event)
        if not note and not image_paths:
            yield event.plain_result("请附上穿搭图片或文字描述")
            return

        yield event.plain_result("正在整理穿搭细节并放进衣柜...")
        try:
            description = await self.generator._describe_images(
                image_paths,
                user_text=note,
                sid=f"life_wardrobe_vision_{event.session_id}",
            )
            entry = self.wardrobe_mgr.add(description, note=note)
        except Exception as exc:
            logger.error("Failed to add wardrobe entry: %s", exc)
            yield event.plain_result(f"放进衣柜失败：{exc}")
            return

        yield event.plain_result(f"已放进衣柜：\n{entry['description']}")

    @filter.command("查看衣柜", alias={"life wardrobe show"})
    async def wardrobe_show(self, event: AstrMessageEvent):
        """查看已保存的穿搭方案。"""
        entries = self.wardrobe_mgr.display_entries(limit=20)
        if not entries:
            yield event.plain_result("衣柜还是空的")
            return
        template = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 36px;
      background: #f4f0e9;
      color: #2d2925;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    .header { margin: 0 0 24px; }
    .title { margin: 0; font-size: 34px; letter-spacing: 1px; }
    .subtitle { margin: 8px 0 0; color: #766f68; font-size: 16px; }
    .entry {
      display: flex;
      gap: 18px;
      margin: 16px 0;
      padding: 22px;
      border: 1px solid #ded6cc;
      border-radius: 16px;
      background: #fffdf9;
      box-shadow: 0 4px 12px rgba(79, 63, 48, .08);
    }
    .number {
      flex: 0 0 52px;
      width: 52px;
      height: 52px;
      border-radius: 50%;
      background: #b86b4b;
      color: #fffaf4;
      font-size: 25px;
      font-weight: 700;
      line-height: 52px;
      text-align: center;
    }
    .content { flex: 1; min-width: 0; }
    .entry-title { margin: 2px 0 10px; font-size: 22px; }
    .description { margin: 0; font-size: 17px; line-height: 1.7; white-space: pre-wrap; }
    .note { margin: 12px 0 0; color: #766f68; font-size: 14px; }
  </style>
</head>
<body>
  <header class="header">
    <h1 class="title">衣柜穿搭方案</h1>
    <p class="subtitle">发送“重写日程 穿搭N”即可选择对应方案</p>
  </header>
  {% for entry in entries %}
  <section class="entry">
    <div class="number">{{ loop.index }}</div>
    <div class="content">
      <h2 class="entry-title">穿搭 {{ loop.index }}</h2>
      <p class="description">{{ entry.description }}</p>
      {% if entry.note %}<p class="note">备注：{{ entry.note }}</p>{% endif %}
    </div>
  </section>
  {% endfor %}
</body>
</html>
"""
        try:
            image_path = await self.html_render(
                template,
                {"entries": entries},
                return_url=False,
                options={"type": "png", "quality": 90},
            )
            yield event.image_result(str(image_path))
        except Exception as exc:
            logger.warning(
                "Failed to render wardrobe image, falling back to text: %s", exc
            )
            lines = ["👗 衣柜穿搭方案："]
            for index, entry in enumerate(entries, start=1):
                lines.append(f"{index}. {entry['description']}")
            yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="life_wardrobe_add")
    async def llm_wardrobe_add(
        self,
        event: AstrMessageEvent,
        description: str = "",
        note: str = "",
    ) -> str:
        """把当前消息中的穿搭图片或文字整理后加入衣柜。

        Args:
            description(string): 要加入衣柜的穿搭描述；如果当前消息有图片，可以为空。
            note(string): 用户对要加入衣柜的穿搭补充说明，可以为空。
        """
        if not event.is_admin():
            return "未执行：只有管理员可以修改衣柜。"
        image_paths = await self._collect_image_paths(event)
        description = str(description or "").strip()
        note = str(note or "").strip()
        request_text = "；".join(item for item in (description, note) if item)

        if not image_paths and not request_text:
            today = resolve_business_now(self.config.get("schedule_time"))
            current = self.data_mgr.get(today)
            if current and current.status == "ok" and current.outfit.strip():
                request_text = current.outfit.strip()
            else:
                return (
                    "未执行：当前消息没有穿搭图片或文字描述，也没有可复用的今日穿搭。"
                )
        try:
            if image_paths:
                normalized = await self.generator._describe_images(
                    image_paths,
                    user_text=request_text,
                    sid=f"life_wardrobe_tool_{event.session_id}",
                )
            else:
                normalized = await self.generator._call_llm(
                    "请把下面的穿搭文字整理成可长期复用的详细穿搭方案，只输出纯文本，不要 Markdown、JSON、解释或寒暄。\n"
                    "按整体风格、上装、下装、外套、鞋袜、配饰、颜色材质、版型与搭配关系详细描述；"
                    "保留原文中的明确约束，不要臆造原文没有的信息。\n"
                    f"原始穿搭：{request_text}",
                    sid=f"life_wardrobe_tool_{event.session_id}",
                )
            entry = self.wardrobe_mgr.add(normalized, note=note)
        except Exception as exc:
            logger.error("LLM wardrobe add failed: %s", exc)
            return f"衣柜写入失败：{exc}"
        return f"已新增衣柜方案（id={entry['id']}）：{entry['description']}"

    @filter.llm_tool(name="life_wardrobe_list")
    async def llm_wardrobe_list(self, event: AstrMessageEvent) -> str:
        """列出衣柜中的穿搭方案，供判断用户指的是哪一套。"""
        del event
        entries = self.wardrobe_mgr.all()
        if not entries:
            return "衣柜为空。"
        lines = ["衣柜方案（最新在前）："]
        for index, entry in enumerate(
            self.wardrobe_mgr.display_entries(limit=20), start=1
        ):
            lines.append(f"{index}. id={entry['id']}；{entry['description']}")
        return "\n".join(lines)

    @filter.llm_tool(name="life_wardrobe_remove")
    async def llm_wardrobe_remove(self, event: AstrMessageEvent, query: str) -> str:
        """从衣柜删除一套不喜欢或不再使用的穿搭方案。

        Args:
            query(string): 方案 id、编号或描述中的关键词。
        """
        if not event.is_admin():
            return "未执行：只有管理员可以修改衣柜。"
        entry = self.wardrobe_mgr.find_for_user_query(query)
        if entry is None:
            return f"没有找到匹配的衣柜方案：{query}"
        self.wardrobe_mgr.remove(entry["id"])
        return f"已从衣柜删除：{entry['description']}"

    @filter.llm_tool(name="life_wardrobe_edit")
    async def llm_wardrobe_edit(
        self,
        event: AstrMessageEvent,
        query: str,
        instruction: str,
    ) -> str:
        """按用户要求修改衣柜中的一套穿搭方案。

        Args:
            query(string): 方案 id、编号或描述中的关键词。
            instruction(string): 对该方案的修改要求，例如不要穿鞋、换成裸足。
        """
        if not event.is_admin():
            return "未执行：只有管理员可以修改衣柜。"
        entry = self.wardrobe_mgr.find_for_user_query(query)
        if entry is None:
            return f"没有找到匹配的衣柜方案：{query}"

        prompt = (
            "请编辑下面的穿搭方案，只输出编辑后的完整纯文本方案，不要 Markdown 或解释。\n"
            f"原方案：{entry['description']}\n"
            f"用户修改要求：{instruction}\n"
            "保留未被要求修改的细节，明确落实用户要求；如果要求裸足，必须明确写出不穿鞋袜、赤足。"
        )
        try:
            description = await self.generator._call_llm(
                prompt,
                sid=f"life_wardrobe_edit_{event.session_id}",
            )
            updated = self.wardrobe_mgr.replace(entry["id"], description)
        except Exception as exc:
            logger.error("LLM wardrobe edit failed: %s", exc)
            return f"衣柜方案修改失败：{exc}"
        return f"已修改衣柜方案（id={updated['id']}）：{updated['description']}"

    @filter.llm_tool(name="life_schedule_edit")
    async def llm_schedule_edit(self, event: AstrMessageEvent, instruction: str) -> str:
        """按用户要求编辑今天已生成的穿搭或日程。

        Args:
            instruction(string): 对今天方案的修改要求，例如删除某件衣服或调整活动。
        """
        if not event.is_admin():
            return "未执行：只有管理员可以修改今日计划。"
        today = resolve_business_now(self.config.get("schedule_time"))
        data = self.data_mgr.get(today)
        if not data:
            try:
                data = await self.generator.generate_schedule(
                    today, event.unified_msg_origin
                )
            except Exception as exc:
                return f"当前没有可编辑的日程，且生成失败：{exc}"

        prompt = (
            "请编辑今天的生活日程，只输出 JSON 对象本体，不要 Markdown 或解释。\n"
            f"当前穿搭：{data.outfit}\n"
            f"当前日程：{data.schedule}\n"
            f"用户修改要求：{instruction}\n"
            'JSON 必须包含 "outfit" 和 "schedule" 两个字符串字段。'
        )
        try:
            content = await self.generator._call_llm(
                prompt,
                sid=f"life_schedule_edit_{event.session_id}",
            )
            payload = self.generator._extract_json_obj(content)
            if (
                not payload
                or not str(payload.get("outfit", "")).strip()
                or not str(payload.get("schedule", "")).strip()
            ):
                return "模型没有返回完整的 outfit/schedule，未修改今日计划。"
            data.outfit = str(payload["outfit"]).strip()
            data.schedule = str(payload["schedule"]).strip()
            self.data_mgr.set(data)
        except Exception as exc:
            logger.error("LLM schedule edit failed: %s", exc)
            return f"今日计划修改失败：{exc}"
        return f"今日计划已更新：\n穿搭：{data.outfit}\n日程：{data.schedule}"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("参考会话", alias={"life umo"})
    async def life_reference_umo(
        self, event: AstrMessageEvent, param: GreedyStr = GreedyStr
    ):
        """参考会话 [set|show|clear]，设置默认参考会话来源。"""
        action = str(param or "set").strip().lower()
        if action in {"", "set"}:
            umo = str(event.unified_msg_origin or "").strip()
            if not umo:
                yield event.plain_result("当前事件没有可保存的会话来源")
                return
            self.config["default_reference_umo"] = umo
            self._save_config()
            yield event.plain_result(
                f"已保存默认参考会话：{self._format_reference_umo(umo)}"
            )
            return
        if action == "show":
            umo = str(self.config.get("default_reference_umo", "") or "").strip()
            if umo:
                yield event.plain_result(
                    f"默认参考会话已配置：{self._format_reference_umo(umo, truncate=False)}"
                )
            else:
                yield event.plain_result("默认参考会话未配置")
            return
        if action == "clear":
            self.config["default_reference_umo"] = ""
            self._save_config()
            yield event.plain_result("已清除默认参考会话")
            return
        yield event.plain_result("用法：参考会话 [set|show|clear]")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程时间", alias={"life time"})
    async def life_time(self, event: AstrMessageEvent, param: str | None = None):
        """日程时间 [HH:MM] ，设置每日日程生成时间"""
        if not param:
            yield event.plain_result("请提供时间，格式为 HH:MM，例如 /life time 07:30")
            return

        # 支持 1~2 位小时、1~2 位分钟，中间用冒号分隔
        if not re.match(r"^\d{1,2}:\d{1,2}$", param):
            yield event.plain_result("时间格式错误，请使用 HH:MM 格式")
            return

        # 再补一层范围校验，防止 99:99 这类非法时间
        try:
            hour, minute = map(int, param.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            yield event.plain_result(
                "时间格式错误，请使用 HH:MM 格式，且小时 0-23、分钟 0-59"
            )
            return

        try:
            self.scheduler.update_schedule_time(param)
            yield event.plain_result(f"已将每日日程生成时间更新为 {param}。")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")
