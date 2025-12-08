import json
import os
import re
import datetime
import asyncio
import aiofiles
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Literal, Callable, Awaitable

try:
    import holidays
except ImportError:
    holidays = None

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api import logger
from astrbot.api.all import Star, Context, Plain, Image
from astrbot.core.star.star_tools import StarTools
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
        if not isinstance(data, dict):
            return ChatReference(umo="")
        return ChatReference(
            umo=str(data.get("umo", "")),
            count=int(data.get("count", 20))
        )
    
    def to_dict(self) -> dict:
        return {"umo": self.umo, "count": self.count}

@dataclass
class SchedulerConfig:
    schedule_time: str = "07:00"
    reference_history_days: int = 3
    reference_chats: List[ChatReference] = field(default_factory=list)
    push_targets: List[str] = field(default_factory=list) # List of umo to push schedule automatically
    prompt_template: str = """# Role: Life Scheduler
请根据以下信息，为自己规划一份今天的生活安排。请代入你的人设，生成的内容应富有生活气息，避免机械的流水账。

## Context
- 日期：{date_str} {weekday} {holiday}
- 人设：{persona_desc}
- 历史日程参考（最近几天）：
{history_schedules}
- 近期对话记忆（参考这些话题来安排相关活动）：
{recent_chats}

## Tasks
1. outfit: 设计今日穿搭。{outfit_desc}
2. schedule: 规划今日日程。包含早中晚的关键活动和心境，可以是工作学习，也可以是娱乐放松，请根据日期属性（工作日/周末/节日）合理安排。

## Output Format
请务必严格遵循 JSON 格式返回，不要包含 Markdown 代码块标记（如 ```json），也不要包含任何额外的解释文本。
格式如下：
{{
    "outfit": "一句话描述穿搭",
    "schedule": "一段话描述今日日程"
}}
"""
    outfit_desc: str = "一句话描述，结合天气、心情和今日活动。"

    @staticmethod
    def from_dict(data: dict) -> 'SchedulerConfig':
        config = SchedulerConfig()
        config.schedule_time = data.get("schedule_time", "07:00")
        config.reference_history_days = data.get("reference_history_days", 3)
        
        refs = data.get("reference_chats", [])
        if isinstance(refs, list):
            config.reference_chats = [ChatReference.from_dict(r) for r in refs if isinstance(r, dict)]
        
        config.push_targets = data.get("push_targets", [])
        if not isinstance(config.push_targets, list):
            config.push_targets = []
        
        if "prompt_template" in data:
            config.prompt_template = data["prompt_template"]
        if "outfit_desc" in data:
            config.outfit_desc = data["outfit_desc"]
            
        return config

    def to_dict(self) -> dict:
        return {
            "schedule_time": self.schedule_time,
            "reference_history_days": self.reference_history_days,
            "reference_chats": [r.to_dict() for r in self.reference_chats],
            "push_targets": self.push_targets,
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
        logger.error(f"Failed to get recent chats for {umo}: {e}")
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


# --- Scheduler Class ---

class LifeScheduler:
    def __init__(self, schedule_time: str, task: Callable[[], Awaitable[None]]):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
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
            logger.info(f"Life Scheduler started at {hour}:{minute}")
        except Exception as e:
            logger.error(f"Failed to setup scheduler: {e}")

    def update_schedule_time(self, new_time: str):
        if new_time == self.schedule_time:
            return
        
        try:
            hour, minute = new_time.split(":")
            self.schedule_time = new_time
            if self.job:
                self.job.reschedule('cron', hour=hour, minute=minute)
                logger.info(f"Life Scheduler rescheduled to {hour}:{minute}")
        except Exception as e:
            logger.error(f"Failed to update scheduler: {e}")

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

# --- Main Class ---

class Main(Star):
    def __init__(self, context: Context, *args, **kwargs) -> None:
        super().__init__(context)
        self.context = context
        
        self.base_dir = StarTools.get_data_dir("astrbot_plugin_life_scheduler")
        self.config_path = self.base_dir / "config.json"
        self.data_path = self.base_dir / "data.json"
        
        self.generation_lock = asyncio.Lock()
        self.failed_dates = set() # Track dates where generation failed to avoid infinite retries
        
        self.config = self.load_config()
        self.schedule_data = self.load_data()
        
        self.scheduler = LifeScheduler(self.config.schedule_time, self.daily_schedule_task)
        self.scheduler.start()

    def load_config(self) -> SchedulerConfig:
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return SchedulerConfig.from_dict(data)
            except json.JSONDecodeError:
                logger.error(f"Config file is corrupted: {self.config_path}")
            except Exception as e:
                logger.exception(f"Failed to load config: {e}")
        return SchedulerConfig()

    async def save_config(self):
        try:
            # Atomic write
            temp_path = self.config_path.with_suffix(".tmp")
            async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(self.config.to_dict(), indent=4, ensure_ascii=False))
            
            if os.name == 'nt' and self.config_path.exists():
                 os.remove(self.config_path) # Windows replace fix
            os.replace(temp_path, self.config_path)
        except Exception as e:
            logger.exception(f"Failed to save config: {e}")

    def load_data(self) -> Dict[str, Any]:
        if self.data_path.exists():
            try:
                with open(self.data_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Data file is corrupted: {self.data_path}")
            except Exception as e:
                logger.exception(f"Failed to load data: {e}")
        return {}

    async def save_data(self):
        try:
            # Atomic write
            temp_path = self.data_path.with_suffix(".tmp")
            async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(self.schedule_data, indent=4, ensure_ascii=False))
            
            if os.name == 'nt' and self.data_path.exists():
                 os.remove(self.data_path) # Windows replace fix
            os.replace(temp_path, self.data_path)
        except Exception as e:
            logger.exception(f"Failed to save data: {e}")

    async def daily_schedule_task(self):
        """定时任务：生成日程"""
        logger.info("Starting daily schedule generation task...")
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        schedule_info = await self.generate_schedule_with_llm()
        if not schedule_info:
            logger.error("Failed to generate schedule.")
            return

        self.schedule_data[today_str] = schedule_info
        await self.save_data()
        
        # 自动推送
        for target in self.config.push_targets:
            await self.send_schedule_info(schedule_info, target)

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
        persona_desc = "你是一个充满活力、热爱生活、情感丰富的AI伙伴。"
        
        # 尝试从 PersonaManager 获取当前人设
        if hasattr(self.context, "persona_manager"):
            try:
                persona = await self.context.persona_manager.get_default_persona_v3()
                # 兼容 dict 访问和属性访问
                if hasattr(persona, "get"):
                    p_prompt = persona.get("prompt", "")
                elif hasattr(persona, "prompt"):
                    p_prompt = persona.prompt
                else:
                    p_prompt = ""
                
                if p_prompt:
                    persona_desc = p_prompt
            except Exception as e:
                logger.warning(f"Failed to get persona from manager: {e}")

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
                logger.error("No LLM provider available.")
                return None
            
            # session_id 必须是 str，如果没有特定会话，可以传空字符串或特定标识
            # 使用特定 session_id 来隔离上下文
            gen_session_id = "life_scheduler_gen"
            try:
                response = await provider.text_chat(prompt, session_id=gen_session_id)
                content = response.completion_text
                
                # JSON 提取
                # Improved JSON extraction: Non-greedy match for first JSON object
                match = re.search(r'\{[\s\S]*?\}', content)
                if match:
                    json_str = match.group(0)
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to decode JSON from LLM: {json_str}")
                        # Fallback
                        return {"outfit": "日常休闲装", "schedule": content}
                else:
                    logger.warning(f"LLM response not in JSON format: {content}")
                    return {"outfit": "日常休闲装", "schedule": content}
            finally:
                # 任务完成后，清理该临时会话的历史记录，防止上下文无限增长
                try:
                    # life_scheduler_gen 作为 UMO，会创建一个 Conversation
                    cid = await self.context.conversation_manager.get_curr_conversation_id(gen_session_id)
                    if cid:
                        await self.context.conversation_manager.delete_conversation(gen_session_id, cid)
                        logger.debug(f"Cleaned up temporary session: {gen_session_id}")
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup temporary session: {cleanup_error}")

        except Exception as e:
            logger.exception(f"Error calling LLM: {e}")
            return None

    async def send_schedule_info(self, schedule_info: Dict[str, str], target_umo: str):
        """发送日程信息"""
        if not target_umo:
            return

        # 准备内容
        text_content = f"📅 {datetime.datetime.now().strftime('%Y-%m-%d')}\n👗 今日穿搭：{schedule_info.get('outfit')}\n📝 日程安排：\n{schedule_info.get('schedule')}"
        
        try:
            # 统一使用 context.send_message，它会自动处理不同平台的适配
            # 注意：send_message 通常接受 MessageChain 对象
            await self.context.send_message(target_umo, MessageChain([Plain(text_content)]))
                
            logger.info(f"Sent schedule to {target_umo}")
        except Exception as e:
            logger.exception(f"Failed to send schedule to {target_umo}: {e}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """System Prompt 注入 & 懒加载"""
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # Double-check locking pattern for lazy loading
        if today_str not in self.schedule_data and today_str not in self.failed_dates:
            async with self.generation_lock:
                # Re-check inside lock
                if today_str not in self.schedule_data and today_str not in self.failed_dates:
                    logger.info(f"Lazy loading schedule for {today_str}...")
                    schedule_info = await self.generate_schedule_with_llm()
                    if schedule_info:
                        self.schedule_data[today_str] = schedule_info
                        await self.save_data()
                    else:
                        logger.warning(f"Failed to lazy load schedule for {today_str}. Marking as failed to prevent infinite retries.")
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
    async def life_command(self, event: AstrMessageEvent, action: str = "", param: str = ""):
        """
        生活日程管理指令
        /life show - 查看今日日程
        /life regenerate - 重新生成今日日程
        /life on - 开启当前会话的自动推送
        /life off - 关闭当前会话的自动推送
        /life time [HH:MM] - 设置每日生成时间
        """
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        umo = event.unified_msg_origin
        
        if action == "show":
            info = self.schedule_data.get(today_str)
            if info:
                await self.send_schedule_info(info, umo)
            else:
                # 尝试生成
                event.set_result(MessageEventResult().message("今日尚未生成日程，正在为您生成..."))
                schedule_info = await self.generate_schedule_with_llm()
                if schedule_info:
                    self.schedule_data[today_str] = schedule_info
                    await self.save_data()
                    await self.send_schedule_info(schedule_info, umo)
                else:
                    event.set_result(MessageEventResult().message("生成失败，请检查日志。"))
        
        elif action == "regenerate":
            schedule_info = await self.generate_schedule_with_llm()
            if schedule_info:
                self.schedule_data[today_str] = schedule_info
                await self.save_data()
                await self.send_schedule_info(schedule_info, umo)
            else:
                event.set_result(MessageEventResult().message("生成失败，请检查日志。"))
        
        elif action == "on":
            if umo not in self.config.push_targets:
                self.config.push_targets.append(umo)
                await self.save_config()
                event.set_result(MessageEventResult().message("已开启当前会话的自动日程推送。"))
            else:
                event.set_result(MessageEventResult().message("当前会话已开启自动推送。"))
        
        elif action == "off":
            if umo in self.config.push_targets:
                self.config.push_targets.remove(umo)
                await self.save_config()
                event.set_result(MessageEventResult().message("已关闭当前会话的自动日程推送。"))
            else:
                event.set_result(MessageEventResult().message("当前会话未开启自动推送。"))
        
        elif action == "time":
            if not param:
                 event.set_result(MessageEventResult().message("请提供时间，格式为 HH:MM，例如 /life time 07:30"))
                 return
            
            if not re.match(r"^\d{2}:\d{2}$", param):
                event.set_result(MessageEventResult().message("时间格式错误，请使用 HH:MM 格式。"))
                return
            
            try:
                self.scheduler.update_schedule_time(param)
                self.config.schedule_time = param
                await self.save_config()
                event.set_result(MessageEventResult().message(f"已将每日日程生成时间更新为 {param}。"))
            except Exception as e:
                event.set_result(MessageEventResult().message(f"设置失败: {e}"))

        else:
            event.set_result(MessageEventResult().message(
                "指令用法：\n"
                "/life show - 查看日程\n"
                "/life regenerate - 重新生成\n"
                "/life on - 开启自动推送\n"
                "/life off - 关闭自动推送\n"
                "/life time <HH:MM> - 设置生成时间"
            ))

    async def terminate(self):
        """插件卸载时清理"""
        self.scheduler.shutdown()
