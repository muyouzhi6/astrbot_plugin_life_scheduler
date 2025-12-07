import json
import logging
import os
import re
import datetime
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Literal, Callable, Awaitable

try:
    import holidays
except ImportError:
    holidays = None

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.event.filter as filter
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.all import Star, Context, Plain, Image
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core import html_renderer

# --- Config Definitions ---

@dataclass
class ChatReference:
    umo: str  # unified_msg_origin
    count: int = 20
    
    @staticmethod
    def from_dict(data: dict) -> 'ChatReference':
        return ChatReference(
            umo=data.get("umo", ""),
            count=data.get("count", 20)
        )
    
    def to_dict(self) -> dict:
        return {"umo": self.umo, "count": self.count}

@dataclass
class SchedulerConfig:
    admin_id: str = "" # 保留兼容
    schedule_time: str = "07:00"
    enable_auto_report: bool = True
    reference_history_days: int = 3
    reference_chats: List[ChatReference] = field(default_factory=list)
    report_targets: List[str] = field(default_factory=list) # List of unified_msg_origin or user_id
    report_mode: Literal["text", "image", "llm_voice", "all"] = "text"
    prompt_template: str = """请根据以下信息，为自己生成一份今天的拟人化生活安排：
日期：{date_str} {weekday} {holiday}
人设：{persona_desc}
参考历史日程：{history_schedules}
参考近期对话：{recent_chats}

请生成以下内容，并以 JSON 格式返回：
1. outfit: {outfit_desc}
2. schedule: 今日日程表（包含早中晚的关键活动，富有生活气息）。

返回格式示例（仅返回 JSON）：
{{
    "outfit": "...",
    "schedule": "..."
}}
"""
    outfit_desc: str = "今日穿搭描述（一句话，符合天气和心情）。"

    @staticmethod
    def from_dict(data: dict) -> 'SchedulerConfig':
        config = SchedulerConfig()
        config.admin_id = data.get("admin_id", "")
        config.schedule_time = data.get("schedule_time", "07:00")
        config.enable_auto_report = data.get("enable_auto_report", True)
        config.reference_history_days = data.get("reference_history_days", 3)
        
        refs = data.get("reference_chats", [])
        config.reference_chats = [ChatReference.from_dict(r) for r in refs]
        
        config.report_targets = data.get("report_targets", [])
        # 兼容旧版 admin_id
        if config.admin_id and config.admin_id not in config.report_targets:
            config.report_targets.append(config.admin_id)
            
        config.report_mode = data.get("report_mode", "text")
        if "prompt_template" in data:
            config.prompt_template = data["prompt_template"]
        if "outfit_desc" in data:
            config.outfit_desc = data["outfit_desc"]
            
        return config

    def to_dict(self) -> dict:
        return {
            "admin_id": self.admin_id,
            "schedule_time": self.schedule_time,
            "enable_auto_report": self.enable_auto_report,
            "reference_history_days": self.reference_history_days,
            "reference_chats": [r.to_dict() for r in self.reference_chats],
            "report_targets": self.report_targets,
            "report_mode": self.report_mode,
            "prompt_template": self.prompt_template,
            "outfit_desc": self.outfit_desc
        }

# --- Helper Functions ---

async def get_recent_chats(context: Context, umo: str, count: int) -> str:
    """获取指定会话的最近聊天记录"""
    try:
        # 尝试从 conversation_manager 获取
        # session = MessageSesion.from_str(umo) # unused
        # 1. 获取当前 conversation_id
        cid = await context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            return "无最近对话记录"
            
        # 2. 获取 conversation
        conv = await context.conversation_manager.get_conversation(umo, cid)
        if not conv or not conv.history:
            return "无最近对话记录"
            
        # 3. 解析 history
        history = json.loads(conv.history)
        
        # 4. 取最近 count 条
        recent = history[-count:] if count > 0 else []
        
        # 5. 格式化
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
        logging.getLogger("astrbot_plugin_life_scheduler").error(f"Failed to get recent chats for {umo}: {e}")
        return "获取对话记录失败"

def get_holiday_info(date: datetime.date) -> str:
    """获取节日信息（中国）"""
    if holidays is None:
        return ""
    
    try:
        cn_holidays = holidays.CN()
        holiday_name = cn_holidays.get(date)
        if holiday_name:
            return f"今天是 {holiday_name}"
    except Exception:
        return ""
    return ""

async def render_schedule_image(context: Context, schedule_data: dict) -> Optional[str]:
    """渲染日程图片，返回图片 URL 或本地路径"""
    try:
        html_template = """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <title>每日日程</title>
            <style>
                body { font-family: 'Microsoft YaHei', sans-serif; background-color: #f0f2f5; padding: 20px; }
                .card { background: white; border-radius: 15px; padding: 30px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); max-width: 600px; margin: 0 auto; }
                .header { text-align: center; margin-bottom: 20px; border-bottom: 2px solid #eee; padding-bottom: 15px; }
                .date { font-size: 1.2em; color: #666; }
                .outfit { background: #e3f2fd; padding: 15px; border-radius: 10px; margin-bottom: 20px; color: #1565c0; }
                .schedule { white-space: pre-wrap; line-height: 1.6; color: #333; }
            </style>
        </head>
        <body>
            <div class="card">
                <div class="header">
                    <h1>📅 今日生活安排</h1>
                    <div class="date">{{ date_str }}</div>
                </div>
                <div class="outfit">
                    <strong>👗 今日穿搭：</strong><br>
                    {{ outfit }}
                </div>
                <div class="schedule">
                    <strong>📝 日程安排：</strong><br>
                    {{ schedule }}
                </div>
            </div>
        </body>
        </html>
        """
        
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        html = html_template.replace("{{ date_str }}", today_str)
        html = html.replace("{{ outfit }}", schedule_data.get("outfit", ""))
        html = html.replace("{{ schedule }}", schedule_data.get("schedule", ""))
        
        url = await html_renderer.render(html)
        return url
        
    except Exception as e:
        logging.getLogger("astrbot_plugin_life_scheduler").error(f"Failed to render image: {e}")
        return None

# --- Scheduler Class ---

class LifeScheduler:
    def __init__(self, schedule_time: str, task: Callable[[], Awaitable[None]]):
        self.scheduler = AsyncIOScheduler()
        self.schedule_time = schedule_time
        self.task = task
        self.job = None

    def start(self):
        try:
            hour, minute = self.schedule_time.split(":")
            self.job = self.scheduler.add_job(
                self.task,
                'cron',
                hour=hour,
                minute=minute,
                id='daily_schedule_gen'
            )
            self.scheduler.start()
            logging.getLogger("astrbot_plugin_life_scheduler").info(f"Life Scheduler started at {hour}:{minute}")
        except Exception as e:
            logging.getLogger("astrbot_plugin_life_scheduler").error(f"Failed to setup scheduler: {e}")

    def update_schedule_time(self, new_time: str):
        if new_time == self.schedule_time:
            return
        
        try:
            hour, minute = new_time.split(":")
            self.schedule_time = new_time
            if self.job:
                self.job.reschedule('cron', hour=hour, minute=minute)
                logging.getLogger("astrbot_plugin_life_scheduler").info(f"Life Scheduler rescheduled to {hour}:{minute}")
        except Exception as e:
            logging.getLogger("astrbot_plugin_life_scheduler").error(f"Failed to update scheduler: {e}")

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

# --- Main Class ---

class Main(Star):
    def __init__(self, context: Context, *args, **kwargs) -> None:
        super().__init__(context)
        self.context = context
        self.logger = logging.getLogger("astrbot_plugin_life_scheduler")
        
        self.base_dir = os.path.dirname(__file__)
        self.config_path = os.path.join(self.base_dir, "config.json")
        self.data_path = os.path.join(self.base_dir, "data.json")
        
        self.generation_lock = asyncio.Lock()
        self.failed_dates = set() # Track dates where generation failed to avoid infinite retries
        
        self.config = self.load_config()
        self.schedule_data = self.load_data()
        
        self.scheduler = LifeScheduler(self.config.schedule_time, self.daily_schedule_task)
        self.scheduler.start()

    def load_config(self) -> SchedulerConfig:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return SchedulerConfig.from_dict(data)
            except Exception as e:
                self.logger.error(f"Failed to load config: {e}")
        return SchedulerConfig()

    def save_config(self):
        try:
            # Atomic write
            temp_path = self.config_path + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.config.to_dict(), f, indent=4, ensure_ascii=False)
            os.replace(temp_path, self.config_path)
        except Exception as e:
            self.logger.error(f"Failed to save config: {e}")

    def load_data(self) -> Dict[str, Any]:
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                self.logger.error(f"Failed to load data: {e}")
        return {}

    def save_data(self):
        try:
            # Atomic write
            temp_path = self.data_path + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self.schedule_data, f, indent=4, ensure_ascii=False)
            os.replace(temp_path, self.data_path)
        except Exception as e:
            self.logger.error(f"Failed to save data: {e}")

    async def daily_schedule_task(self):
        """定时任务：生成日程并尝试播报"""
        self.logger.info("Starting daily schedule generation task...")
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        schedule_info = await self.generate_schedule_with_llm()
        if not schedule_info:
            self.logger.error("Failed to generate schedule.")
            return

        self.schedule_data[today_str] = schedule_info
        self.save_data()
        
        if self.config.enable_auto_report:
            await self.report_schedule(schedule_info)

    async def generate_schedule_with_llm(self) -> Optional[Dict[str, str]]:
        """调用 LLM 生成日程"""
        today = datetime.datetime.now()
        date_str = today.strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        holiday = get_holiday_info(today.date())
        
        # 1. 收集上下文
        # 历史日程
        history_schedules = []
        for i in range(1, self.config.reference_history_days + 1):
            past_date = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if past_date in self.schedule_data:
                history_schedules.append(f"[{past_date}]: {self.schedule_data[past_date].get('schedule', '')[:100]}...")
        history_schedules_str = "\n".join(history_schedules) if history_schedules else "无历史记录"

        # 近期对话
        recent_chats_str = ""
        if self.config.reference_chats:
            chats = []
            for ref in self.config.reference_chats:
                chat_content = await get_recent_chats(self.context, ref.umo, ref.count)
                if chat_content:
                    chats.append(f"--- 会话 {ref.umo} ---\n{chat_content}")
            recent_chats_str = "\n".join(chats)
        if not recent_chats_str:
            recent_chats_str = "无近期对话"

        # 2. 构造 Prompt
        persona_desc = "你是一个充满活力、热爱生活的AI助手。"
        
        prompt = self.config.prompt_template.format(
            date_str=date_str,
            weekday=weekday,
            holiday=holiday,
            persona_desc=persona_desc,
            history_schedules=history_schedules_str,
            recent_chats=recent_chats_str,
            outfit_desc=self.config.outfit_desc
        )

        try:
            content = ""
            provider = self.context.get_using_provider()
            if not provider:
                self.logger.error("No LLM provider available.")
                return None
            
            # session_id 必须是 str，如果没有特定会话，可以传空字符串或特定标识
            response = await provider.text_chat(prompt, session_id="life_scheduler_gen")
            content = response.completion_text
            
            # JSON 提取
            # Improved JSON extraction
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                json_str = match.group(0)
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    self.logger.warning(f"Failed to decode JSON from LLM: {json_str}")
                    return {"outfit": "日常休闲装", "schedule": content}
            else:
                self.logger.warning(f"LLM response not in JSON format: {content}")
                return {"outfit": "日常休闲装", "schedule": content}
        except Exception as e:
            self.logger.error(f"Error calling LLM: {e}")
            return None

    async def report_schedule(self, schedule_info: Dict[str, str], target_umo: Optional[str] = None):
        """发送播报"""
        targets = [target_umo] if target_umo else self.config.report_targets
        if not targets:
            self.logger.warning("No report targets configured.")
            return

        msg_chain = []
        mode = self.config.report_mode
        
        # 准备内容
        text_content = f"早安！\n👗 今日穿搭：{schedule_info.get('outfit')}\n📝 日程安排：\n{schedule_info.get('schedule')}"
        
        if mode in ["image", "all"]:
            img_url = await render_schedule_image(self.context, schedule_info)
            if img_url:
                msg_chain.append(Image(img_url))
            else:
                # 降级为文本
                if mode == "image":
                    msg_chain.append(Plain(text_content))
        
        if mode == "text" or mode == "all":
            msg_chain.append(Plain(text_content))

        if mode == "llm_voice":
            # TODO: 调用 LLM 转述，生成语音（需 TTS 支持）
            # 暂时降级为文本
            msg_chain.append(Plain(text_content))

        # 发送
        for target in targets:
            if not target: continue
            try:
                # 统一使用 context.send_message，它会自动处理不同平台的适配
                # 注意：send_message 通常接受 MessageChain 对象
                await self.context.send_message(target, MessageChain(msg_chain))
                    
                self.logger.info(f"Reported schedule to {target}")
            except Exception as e:
                self.logger.error(f"Failed to report to {target}: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入 & 懒加载"""
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # Double-check locking pattern for lazy loading
        if today_str not in self.schedule_data and today_str not in self.failed_dates:
            async with self.generation_lock:
                # Re-check inside lock
                if today_str not in self.schedule_data and today_str not in self.failed_dates:
                    self.logger.info(f"Lazy loading schedule for {today_str}...")
                    schedule_info = await self.generate_schedule_with_llm()
                    if schedule_info:
                        self.schedule_data[today_str] = schedule_info
                        self.save_data()
                    else:
                        self.logger.warning(f"Failed to lazy load schedule for {today_str}. Marking as failed to prevent infinite retries.")
                        self.failed_dates.add(today_str)
        
        if today_str in self.schedule_data:
            info = self.schedule_data[today_str]
            now_hour = datetime.datetime.now().hour
            status = "进行中"
            if now_hour < 9: status = "刚开始"
            elif now_hour > 22: status = "即将结束"
            
            inject_text = f"\n[今日生活状态 ({status})]\n穿搭：{info.get('outfit')}\n日程：{info.get('schedule')}\n请在回答中体现这些生活状态。"
            req.system_prompt += inject_text

    @filter.command("life")
    async def life_command(self, event: AstrMessageEvent, action: str = ""):
        """
        生活日程管理指令
        /life show - 查看今日日程
        /life regenerate - 重新生成今日日程
        """
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        if action == "show":
            info = self.schedule_data.get(today_str)
            if info:
                await self.report_schedule(info, event.unified_msg_origin)
            else:
                event.set_result(MessageEventResult().message("今日尚未生成日程。"))
        
        elif action == "regenerate":
            event.set_result(MessageEventResult().message("正在重新生成日程，请稍候..."))
            schedule_info = await self.generate_schedule_with_llm()
            if schedule_info:
                self.schedule_data[today_str] = schedule_info
                self.save_data()
                await self.report_schedule(schedule_info, event.unified_msg_origin)
            else:
                event.set_result(MessageEventResult().message("生成失败，请检查日志。"))
        
        else:
            event.set_result(MessageEventResult().message("指令用法：\n/life show - 查看日程\n/life regenerate - 重新生成"))

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.shutdown()
