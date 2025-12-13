import json
import os
import re
import datetime
import asyncio
import aiohttp
import aiofiles
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable, Awaitable
try:
    import holidays
except ImportError:
    holidays = None
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot.core.provider.entities import ProviderRequest

WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

WEEK_TEMPLATES = {
    "regular": {"name": "常规周", "emoji": "📊", "description": "普通的一周",
        "hints": {"monday": "新的一周开始", "tuesday": "进入状态", "wednesday": "周中保持节奏", "thursday": "继续推进", "friday": "收尾工作", "saturday": "自由安排", "sunday": "休息充电"},
        "suggested_activities": {"monday": ["整理计划"], "tuesday": ["专注工作"], "wednesday": ["日常任务"], "thursday": ["推进项目"], "friday": ["收尾"], "saturday": ["出门逛逛"], "sunday": ["休息"]}},
    "sprint": {"name": "冲刺周", "emoji": "🚀", "description": "有重要目标的一周",
        "hints": {"monday": "明确目标", "tuesday": "专注推进", "wednesday": "检查进度", "thursday": "最后冲刺", "friday": "收尾验收", "saturday": "彻底放松", "sunday": "恢复休息"},
        "suggested_activities": {"monday": ["制定计划"], "tuesday": ["核心任务"], "wednesday": ["检查进度"], "thursday": ["冲刺"], "friday": ["庆祝"], "saturday": ["放松"], "sunday": ["复盘"]}},
    "relax": {"name": "放松周", "emoji": "🌴", "description": "享受生活的一周",
        "hints": {"monday": "慢慢来", "tuesday": "做喜欢的事", "wednesday": "约朋友", "thursday": "探索新事物", "friday": "继续享受", "saturday": "出门走走", "sunday": "安静充电"},
        "suggested_activities": {"monday": ["睡懒觉"], "tuesday": ["兴趣爱好"], "wednesday": ["约朋友"], "thursday": ["探店"], "friday": ["看电影"], "saturday": ["逛街"], "sunday": ["宅家"]}},
}

def get_week_id(date=None):
    if date is None: date = datetime.datetime.now()
    return date.strftime("%Y-W%W")

def get_monday_of_week(date=None):
    if date is None: date = datetime.datetime.now()
    return (date - datetime.timedelta(days=date.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

@dataclass
class ChatReference:
    umo: str
    count: int = 20
    @staticmethod
    def from_dict(data):
        if not isinstance(data, dict): return ChatReference(umo="")
        return ChatReference(umo=str(data.get("umo", "")), count=int(data.get("count", 20)))

@dataclass
class WeatherConfig:
    api_key: str = ""
    api_host: str = ""
    default_city: str = ""
    @staticmethod
    def from_dict(data):
        if not isinstance(data, dict): return WeatherConfig()
        return WeatherConfig(api_key=str(data.get("api_key", "")), api_host=str(data.get("api_host", "")), default_city=str(data.get("default_city", "")))

@dataclass
class SchedulerConfig:
    schedule_time: str = "07:00"
    reference_history_days: int = 3
    reference_chats: List[ChatReference] = field(default_factory=list)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    week_plan_enabled: bool = True
    week_plan_day: str = "monday"
    week_plan_time: str = "06:00"
    default_week_template: str = "regular"
    prompt_template: str = """# Role: Life Scheduler
请根据以下信息规划今天的生活安排。
- 日期：{date_str} {weekday} {holiday}
- 天气：{weather}
- 人设：{persona_desc}
- 本周主题：{week_theme}
- 本周目标：{week_goals}
- 今日定位：{today_hint}
- 建议活动：{today_suggested}
- 本周进度：{week_progress}
- 历史日程：{history_schedules}
- 近期对话：{recent_chats}
请生成JSON：{{"outfit": "今日穿搭", "schedule": "今日日程"}}
"""
    outfit_desc: str = "今日穿搭描述"
    
    @staticmethod
    def from_dict(data):
        config = SchedulerConfig()
        if not isinstance(data, dict): return config
        config.schedule_time = data.get("schedule_time", "07:00")
        config.reference_history_days = data.get("reference_history_days", 3)
        refs = data.get("reference_chats", [])
        if isinstance(refs, list):
            config.reference_chats = [ChatReference.from_dict(r) for r in refs if isinstance(r, dict)]
        config.weather = WeatherConfig(api_key=str(data.get("weather_api_key", "")), api_host=str(data.get("weather_api_host", "")), default_city=str(data.get("weather_default_city", "")))
        config.week_plan_enabled = data.get("week_plan_enabled", True)
        config.week_plan_day = data.get("week_plan_day", "monday")
        config.week_plan_time = data.get("week_plan_time", "06:00")
        config.default_week_template = data.get("default_week_template", "regular")
        if "prompt_template" in data: config.prompt_template = data["prompt_template"]
        if "outfit_desc" in data: config.outfit_desc = data["outfit_desc"]
        return config

def extract_json_from_text(text):
    text = re.sub(r'^```json\s*|^```\s*|```\s*$', '', text.strip(), flags=re.MULTILINE)
    start = text.find('{')
    if start == -1: return None
    level, in_str, esc = 0, False, False
    for i, c in enumerate(text[start:], start):
        if in_str:
            if esc: esc = False
            elif c == '\\': esc = True
            elif c == '"': in_str = False
        else:
            if c == '"': in_str = True
            elif c == '{': level += 1
            elif c == '}':
                level -= 1
                if level == 0:
                    try: return json.loads(text[start:i+1])
                    except: pass
    return None

def extract_city_from_persona(persona):
    cities = ["北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉", "西安", "长沙", "重庆", "天津", "苏州", "厦门", "青岛"]
    for c in cities:
        if c in persona: return c
    return ""

async def get_recent_chats(context, umo, count):
    try:
        cid = await context.conversation_manager.get_curr_conversation_id(umo)
        if not cid: return "无"
        conv = await context.conversation_manager.get_conversation(umo, cid)
        if not conv or not conv.history: return "无"
        history = json.loads(conv.history)
        recent = history[-count:] if count > 0 else []
        formatted = [f"{'用户' if m.get('role')=='user' else '我'}: {m.get('content', '')}" for m in recent]
        return "\n".join(formatted) if formatted else "无"
    except: return "无"

def get_holiday_info(date):
    if holidays is None: return ""
    try:
        h = holidays.CN().get(date)
        return f"今天是 {h}" if h else ""
    except: return ""

class WeatherService:
    def __init__(self, config):
        self.config = config
        self._session = None
    
    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed: await self._session.close()
    
    async def get_weather(self, city):
        if not self.config.api_key or not self.config.api_host: return "未配置天气API"
        try:
            session = await self._get_session()
            host = self.config.api_host.replace("https://", "").replace("http://", "").rstrip("/")
            headers = {"X-QW-Api-Key": self.config.api_key}
            async with session.get(f"https://{host}/geo/v2/city/lookup", params={"location": city, "number": 1}, headers=headers) as r:
                if r.status != 200: return "城市查询失败"
                d = await r.json()
                if d.get("code") != "200" or not d.get("location"): return f"未找到城市: {city}"
                loc_id = d["location"][0]["id"]
            async with session.get(f"https://{host}/v7/weather/now", params={"location": loc_id}, headers=headers) as r:
                if r.status != 200: return "天气查询失败"
                d = await r.json()
                if d.get("code") != "200": return "天气查询失败"
                n = d.get("now", {})
                return f"{city}: {n.get('text', '?')}, {n.get('temp', '?')}°C"
        except Exception as e: return f"天气查询失败: {e}"

@register("life_scheduler", "Assistant", "生活日程管理插件", "2.0.0", "repo")
class Main(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.context = context
        self.base_dir = StarTools.get_data_dir("astrbot_plugin_life_scheduler")
        self.data_path = self.base_dir / "data.json"
        self.generation_lock = asyncio.Lock()
        self.data_lock = asyncio.Lock()
        self.failed_dates = set()
        self.config = SchedulerConfig.from_dict(config)
        self.schedule_data = self._load_data_sync()
        self.weather_service = WeatherService(self.config.weather)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._setup_scheduler()
        logger.info("[LifeScheduler] Initialized")
    
    def _setup_scheduler(self):
        try:
            h, m = self.config.schedule_time.split(":")
            self.scheduler.add_job(self._daily_task, 'cron', hour=int(h), minute=int(m), id="daily")
            if self.config.week_plan_enabled:
                wh, wm = self.config.week_plan_time.split(":")
                day_map = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
                self.scheduler.add_job(self._weekly_task, 'cron', day_of_week=day_map.get(self.config.week_plan_day, "mon"), hour=int(wh), minute=int(wm), id="weekly")
            self.scheduler.start()
        except Exception as e:
            logger.error(f"Scheduler setup failed: {e}")
    
    def _load_data_sync(self):
        if self.data_path.exists():
            try:
                with open(self.data_path, 'r', encoding='utf-8') as f: return json.load(f)
            except: pass
        return {}
    
    async def _save_data(self):
        async with self.data_lock:
            try:
                self.base_dir.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(self.data_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(self.schedule_data, indent=2, ensure_ascii=False))
            except Exception as e:
                logger.error(f"Save failed: {e}")
    
    async def _get_persona(self):
        try:
            if hasattr(self.context, "persona_manager"):
                p = await self.context.persona_manager.get_default_persona_v3()
                if hasattr(p, "get"): return p.get("prompt", "")
                if hasattr(p, "prompt"): return p.prompt
        except: pass
        return "一个热爱生活的人"
    
    def _get_week_plan(self):
        week_id = get_week_id()
        plans = self.schedule_data.get("week_plans", {})
        if week_id in plans: return plans[week_id]
        t = WEEK_TEMPLATES.get(self.config.default_week_template, WEEK_TEMPLATES["regular"])
        return {"theme": f"{t['emoji']} {t['name']}", "goals": ["按日常节奏"], "daily_hints": t["hints"], "suggested_activities": t["suggested_activities"], "generated": False}
    
    def _get_week_progress(self):
        monday = get_monday_of_week()
        today = datetime.datetime.now()
        lines = []
        for i in range(7):
            d = monday + datetime.timedelta(days=i)
            if d.date() > today.date(): break
            ds = d.strftime("%Y-%m-%d")
            if ds in self.schedule_data and isinstance(self.schedule_data[ds], dict) and 'schedule' in self.schedule_data[ds]:
                lines.append(f"- {WEEKDAY_CN[i]}: {self.schedule_data[ds]['schedule'][:50]}...")
        return "\n".join(lines) if lines else "本周暂无记录"
    
    async def _daily_task(self):
        logger.info("Running daily task...")
        async with self.generation_lock:
            await self._do_generate_daily(force=True)
    
    async def _weekly_task(self):
        logger.info("Running weekly task...")
        async with self.generation_lock:
            await self._do_generate_week_plan()
    
    async def _do_generate_daily(self, date=None, force=False):
        if date is None: date = datetime.datetime.now()
        date_str = date.strftime("%Y-%m-%d")
        if not force and date_str in self.schedule_data: return self.schedule_data[date_str]
        
        persona = await self._get_persona()
        weekday = WEEKDAY_CN[date.weekday()]
        holiday = get_holiday_info(date.date())
        city = self.config.weather.default_city or extract_city_from_persona(persona) or "北京"
        weather = await self.weather_service.get_weather(city)
        week_plan = self._get_week_plan()
        today_key = WEEKDAY_NAMES[date.weekday()]
        
        history = []
        for i in range(1, self.config.reference_history_days + 1):
            pd = (date - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if pd in self.schedule_data and isinstance(self.schedule_data[pd], dict):
                history.append(f"[{pd}]: {self.schedule_data[pd].get('schedule', '')[:80]}...")
        
        recent_chats = "无"
        if self.config.reference_chats:
            chats = []
            for ref in self.config.reference_chats:
                c = await get_recent_chats(self.context, ref.umo, ref.count)
                if c and c != "无": chats.append(c)
            if chats: recent_chats = "\n".join(chats)
        
        prompt = self.config.prompt_template.format(
            date_str=date.strftime("%Y年%m月%d日"), weekday=weekday, holiday=holiday, weather=weather,
            persona_desc=persona, week_theme=week_plan.get('theme', '常规周'),
            week_goals=', '.join(week_plan.get('goals', [])),
            today_hint=week_plan.get('daily_hints', {}).get(today_key, '普通的一天'),
            today_suggested=', '.join(week_plan.get('suggested_activities', {}).get(today_key, [])),
            week_progress=self._get_week_progress(),
            history_schedules="\n".join(history) if history else "无",
            recent_chats=recent_chats, outfit_desc=self.config.outfit_desc
        )
        
        try:
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("No LLM provider")
                return None
            resp = await provider.text_chat(prompt, session_id="life_scheduler_gen")
            result = extract_json_from_text(resp.completion_text)
            if result:
                result["weather"] = weather
                self.schedule_data[date_str] = result
                await self._save_data()
                logger.info(f"Generated schedule for {date_str}")
                return result
            else:
                logger.error(f"Failed to parse JSON: {resp.completion_text[:200]}")
        except Exception as e:
            logger.error(f"Generate daily failed: {e}")
        return None
    
    async def _do_generate_week_plan(self, template_id=None, goals=""):
        if template_id is None: template_id = self.config.default_week_template
        template = WEEK_TEMPLATES.get(template_id, WEEK_TEMPLATES["regular"])
        week_id = get_week_id()
        monday = get_monday_of_week()
        sunday = monday + datetime.timedelta(days=6)
        persona = await self._get_persona()
        
        prompt = f"""生成本周计划({monday.strftime("%m-%d")}至{sunday.strftime("%m-%d")})
模板：{template['name']} - {template['description']}
人设：{persona[:200]}
目标：{goals if goals else '无特别指定'}
返回JSON：{{"theme": "主题", "goals": ["目标"], "daily_hints": {{"monday": "...", "tuesday": "...", "wednesday": "...", "thursday": "...", "friday": "...", "saturday": "...", "sunday": "..."}}, "suggested_activities": {{"monday": ["活动"], "tuesday": ["活动"], "wednesday": ["活动"], "thursday": ["活动"], "friday": ["活动"], "saturday": ["活动"], "sunday": ["活动"]}}}}"""
        
        try:
            provider = self.context.get_using_provider()
            if not provider: return None
            resp = await provider.text_chat(prompt, session_id="life_scheduler_week")
            result = extract_json_from_text(resp.completion_text)
            if not result:
                result = {"theme": f"{template['emoji']} {template['name']}", "goals": ["按模板节奏"], "daily_hints": template["hints"], "suggested_activities": template["suggested_activities"]}
            result["template_id"] = template_id
            result["generated"] = True
            if "week_plans" not in self.schedule_data: self.schedule_data["week_plans"] = {}
            self.schedule_data["week_plans"][week_id] = result
            await self._save_data()
            logger.info(f"Generated week plan for {week_id}")
            return result
        except Exception as e:
            logger.error(f"Generate week plan failed: {e}")
            return None
    
    def _get_time_status(self):
        """获取时间段状态 [1]"""
        hour = datetime.datetime.now().hour
        if hour < 9:
            return "刚开始"
        elif hour >= 22:
            return "即将结束"
        else:
            return "进行中"
        
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if req.session_id and req.session_id.startswith("life_scheduler"): return
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        if today_str not in self.schedule_data and today_str not in self.failed_dates:
            async with self.generation_lock:
                if today_str not in self.schedule_data:
                    result = await self._do_generate_daily()
                    if not result: self.failed_dates.add(today_str)
        if today_str in self.schedule_data and isinstance(self.schedule_data[today_str], dict):
            info = self.schedule_data[today_str]
            week_plan = self._get_week_plan()
            inject = f"\n[System Info]\n天气：{info.get('weather', '未知')}\n穿搭：{info.get('outfit', '未设定')}\n日程：{info.get('schedule', '未设定')}\n本周：{week_plan.get('theme', '常规周')}"
            req.system_prompt += inject
    
    @filter.command("life")
    async def life_command(self, event: AstrMessageEvent, action: str = "", param: str = ""):
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        if action in ["", "help"]:
            yield event.plain_result("📅 生活日程管理\n/life show - 查看今日\n/life week - 查看周计划\n/life regenerate - 重新生成今日\n/life newweek [模板] [目标] - 生成新周计划\n/life templates - 查看模板\n/life weather [城市] - 查询天气\n/life history [天数] - 历史记录")
            return
        
        if action == "show":
            if today_str in self.schedule_data and isinstance(self.schedule_data[today_str], dict):
                info = self.schedule_data[today_str]
                yield event.plain_result(f"📅 今日日程 ({today_str})\n\n🌤️ 天气：{info.get('weather', '未知')}\n\n👔 穿搭：{info.get('outfit', '未设定')}\n\n📋 日程：{info.get('schedule', '未设定')}")
            else:
                yield event.plain_result(f"今日 ({today_str}) 尚未生成日程。\n使用 /life regenerate 生成。")
            return
        
        if action == "week":
            plan = self._get_week_plan()
            today_key = WEEKDAY_NAMES[datetime.datetime.now().weekday()]
            result = f"📅 本周计划 ({get_week_id()})\n\n🎯 主题：{plan.get('theme', '未设定')}\n\n📌 目标：\n" + "\n".join([f"  • {g}" for g in plan.get('goals', [])])
            result += f"\n\n📍 今日定位：{plan.get('daily_hints', {}).get(today_key, '无')}"
            result += f"\n\n💡 建议活动：{', '.join(plan.get('suggested_activities', {}).get(today_key, []))}"
            result += f"\n\n✅ 本周进度：\n{self._get_week_progress()}"
            yield event.plain_result(result)
            return
        
        if action == "templates":
            lines = ["📚 可用周模板："]
            for tid, t in WEEK_TEMPLATES.items():
                lines.append(f"\n{t['emoji']} {tid}: {t['name']}\n   {t['description']}")
            yield event.plain_result("\n".join(lines))
            return
        
        if action == "newweek":
            parts = param.split(" ", 1) if param else ["", ""]
            template_id = parts[0] if parts[0] in WEEK_TEMPLATES else self.config.default_week_template
            goals = parts[1] if len(parts) > 1 else ""
            yield event.plain_result(f"正在生成周计划（模板: {template_id}）...")
            async with self.generation_lock:
                plan = await self._do_generate_week_plan(template_id, goals)
            if plan:
                yield event.plain_result(f"✅ 周计划已生成！\n\n🎯 主题：{plan.get('theme')}\n📌 目标：{', '.join(plan.get('goals', []))}")
            else:
                yield event.plain_result("❌ 生成失败")
            return
        
        if action == "regenerate":
            yield event.plain_result("正在重新生成...")
            async with self.generation_lock:
                self.failed_dates.discard(today_str)
                if today_str in self.schedule_data: del self.schedule_data[today_str]
                result = await self._do_generate_daily(force=True)
            if result:
                yield event.plain_result(f"✅ 已重新生成！ ({today_str})\n\n🌤️ 天气：{result.get('weather', '未知')}\n\n👔 穿搭：{result.get('outfit', '未设定')}\n\n📋 日程：{result.get('schedule', '未设定')}")
            else:
                yield event.plain_result("❌ 生成失败")
            return
        
        if action == "weather":
            city = param.strip() if param else self.config.weather.default_city
            if not city:
                persona = await self._get_persona()
                city = extract_city_from_persona(persona)
            if not city:
                yield event.plain_result("请指定城市：/life weather 北京")
                return
            weather = await self.weather_service.get_weather(city)
            yield event.plain_result(f"🌤️ {weather}")
            return
        
        if action == "history":
            days = int(param) if param.isdigit() else 7
            results = []
            for i in range(days):
                d = (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                if d in self.schedule_data and isinstance(self.schedule_data[d], dict):
                    results.append(f"📅 {d}\n{self.schedule_data[d].get('schedule', '')[:80]}...")
            yield event.plain_result("\n\n".join(results) if results else f"最近 {days} 天没有记录")
            return
        
        yield event.plain_result("未知指令，使用 /life help 查看帮助")
