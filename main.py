import datetime
import re

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.data import ScheduleData, ScheduleDataManager
from .core.generator import SchedulerGenerator
from .core.schedule import LifeScheduler
from .core.utils import time_desc


class LifeSchedulerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        self.schedule_data_file = self.data_dir / "schedule_data.json"

    async def initialize(self):
        self.data_mgr = ScheduleDataManager(self.schedule_data_file)
        self.generator = SchedulerGenerator(self.context, self.config, self.data_mgr)
        self.scheduler = LifeScheduler(
            context=self.context,
            config=self.config,
            task=self.generator.generate_schedule,
        )
        self.scheduler.start()

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.stop()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入 & 懒加载"""
        # 防止无限递归：如果请求来自本插件的日程生成任务，直接忽略
        if req.session_id == "life_scheduler_gen":
            return

        today = datetime.datetime.now()
        umo = event.unified_msg_origin
        data: ScheduleData = self.data_mgr.get(
            today
        ) or await self.generator.generate_schedule(today, umo)
        if data.status == "failed":
            return

        # 使用更隐晦的方式注入，强调这是内心状态而非需要表达的信息
        inject_text = f"""

[内在状态 - 仅供理解角色当前心境，无需在对话中提及]
时间感知：{time_desc()}
穿着状态：{data.outfit}
当日背景：{data.schedule}

[对话原则]
- 专注于用户当前的话题和需求
- 像真人一样自然对话，不解释自己在做什么、穿什么、在哪里
- 只有当用户明确问起时，才简短回应相关问题
- 上述内在状态只影响你的语气和情绪色彩，不作为对话内容"""

        req.system_prompt += inject_text
        logger.debug(f"[LLM] 添加的内在状态注入：{inject_text}")

    @filter.command("life")
    async def life_command(
        self,
        event: AstrMessageEvent,
        action: str | None = None,
        param: str | None = None,
    ):
        """
        生活日程管理指令
        /life show - 查看今日日程
        /life regenerate - 重新生成今日日程
        /life time [HH:MM] - 设置每日生成时间
        """
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin
        match action:
            case "show":
                data = self.data_mgr.get(
                    today
                ) or await self.generator.generate_schedule(today, umo)
                if not data:
                    yield event.plain_result("今日尚未生成日程，生成失败")
                    return

                yield event.plain_result(
                    f"📅 {today_str}\n👗 今日穿搭：{data.outfit}\n📝 日程安排：\n{data.schedule}"
                )

            case "regenerate":
                yield event.plain_result("正在重新生成今日日程...")
                data = await self.generator.generate_schedule(today, umo)
                if not data:
                    yield event.plain_result("重新生成失败，请查看日志")
                    return
                self.data_mgr.set(data)

                yield event.plain_result(
                    f"📅 {today_str}"
                    f"\n👗 今日穿搭：{data.outfit}"
                    f"\n📝 日程安排：\n{data.schedule}"
                )
            case "time":
                if not param:
                    yield event.plain_result(
                        "请提供时间，格式为 HH:MM，例如 /life time 07:30"
                    )
                elif not re.match(r"^\d{2}:\d{2}$", param):
                    yield event.plain_result("时间格式错误，请使用 HH:MM 格式。")
                else:
                    try:
                        self.scheduler.update_schedule_time(param)
                        self.config["schedule_time"] = param
                        self.config.save_config()
                        yield event.plain_result(
                            f"已将每日日程生成时间更新为 {param}。"
                        )
                    except Exception as e:
                        yield event.plain_result(f"设置失败: {e}")
            case _:
                yield event.plain_result(
                    "指令用法：\n"
                    "/life show - 查看日程\n"
                    "/life regenerate - 重新生成\n"
                    "/life time <HH:MM> - 设置生成时间"
                )
