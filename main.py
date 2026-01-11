import datetime
import re

from astrbot.api import logger
from astrbot.api.all import Context, Star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .core.data import ScheduleDataManager
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
        """System Prompt 注入"""
        today = datetime.datetime.now()
        umo = event.unified_msg_origin
        data = self.data_mgr.get(today)
        if not data:
            try:
                data = await self.generator.generate_schedule(today, umo)
            except RuntimeError:
                return
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

    @filter.command("查看日程", alias={"life show"})
    async def life_show(self, event: AstrMessageEvent):
        """查看今日的日程"""
        today = datetime.datetime.now()
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

    @filter.command("重写日程", alias={"life renew"})
    async def life_renew(self, event: AstrMessageEvent):
        """重写今日的日程"""
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        umo = event.unified_msg_origin
        yield event.plain_result("正在重写今日日程...")
        try:
            data = await self.generator.generate_schedule(today, umo)
        except RuntimeError:
            yield event.plain_result("已有日程生成任务在进行中，请稍后再试")
            return
        yield event.plain_result(
            f"📅 {today_str}"
            f"\n👗 今日穿搭：{data.outfit}"
            f"\n📝 日程安排：\n{data.schedule}"
        )

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
