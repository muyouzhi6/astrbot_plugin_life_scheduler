import datetime
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _install_astrbot_stubs():
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.config": types.ModuleType("astrbot.core.config"),
        "astrbot.core.config.astrbot_config": types.ModuleType(
            "astrbot.core.config.astrbot_config"
        ),
        "astrbot.core.star": types.ModuleType("astrbot.core.star"),
        "astrbot.core.star.context": types.ModuleType("astrbot.core.star.context"),
    }
    modules["astrbot.api"].logger = _Logger()
    modules["astrbot.core.config.astrbot_config"].AstrBotConfig = dict
    modules["astrbot.core.star.context"].Context = object
    sys.modules.update(modules)


_install_astrbot_stubs()

from core.data import ScheduleDataManager  # noqa: E402
from core.generator import ScheduleContext, SchedulerGenerator  # noqa: E402
from core.utils import build_character_state_injection, select_current_activity  # noqa: E402


class _ConversationManager:
    async def get_curr_conversation_id(self, umo):
        return None

    async def delete_conversation(self, sid, cid):
        pass


class _PersonaManager:
    async def get_default_persona_v3(self):
        return {"prompt": "你是一个测试人格。"}


class _Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def text_chat(self, prompt, session_id):
        self.prompts.append(prompt)
        text = self.responses.pop(0) if self.responses else ""
        return types.SimpleNamespace(completion_text=text)


class _Context:
    def __init__(self, provider):
        self.provider = provider
        self.conversation_manager = _ConversationManager()
        self.persona_manager = _PersonaManager()

    def get_provider_by_id(self, provider_id):
        return None

    def get_using_provider(self):
        return self.provider


def _config():
    return {
        "reference_history_days": 3,
        "reference_recent_count": 0,
        "llm_provider": "",
        "pool": {
            "daily_themes": ["探索日"],
            "mood_colors": ["活力"],
            "outfit_styles": ["甜酷混搭风"],
            "schedule_types": ["户外活动型"],
        },
        "prompt_template": (
            "# Role: Life Scheduler\n"
            "- 穿搭风格（必须严格遵循）：【{outfit_style}】\n"
            "- 日程类型：【{schedule_type}】\n"
            "请严格返回 JSON：\n"
            "{{\n"
            '  "outfit_style": "{outfit_style}",\n'
            '  "outfit": "...",\n'
            '  "schedule": "..."\n'
            "}}"
        ),
    }


def _ctx():
    return ScheduleContext(
        date_str="2026年05月24日",
        weekday="星期日",
        holiday="",
        persona_desc="测试人格",
        history_schedules="（无历史记录）",
        recent_chats="无近期对话",
        daily_theme="探索日",
        mood_color="活力",
        outfit_style="甜酷混搭风",
        schedule_type="户外活动型",
    )


class SchedulerBehaviorTest(unittest.IsolatedAsyncioTestCase):
    def _generator(self, responses=()):
        self.tmp = tempfile.TemporaryDirectory()
        data_mgr = ScheduleDataManager(Path(self.tmp.name) / "schedule_data.json")
        provider = _Provider(responses)
        return SchedulerGenerator(_Context(provider), _config(), data_mgr), provider

    def tearDown(self):
        tmp = getattr(self, "tmp", None)
        if tmp:
            tmp.cleanup()

    def test_manual_extra_has_highest_priority_and_skips_style_validation(self):
        generator, _ = self._generator()
        prompt = generator._build_prompt(_ctx(), "穿黑丝和吊带裙")

        self.assertIn("用户补充要求：穿黑丝和吊带裙", prompt)
        self.assertIn("最高优先级", prompt)
        self.assertIn("不得忽略、替换、弱化", prompt)
        self.assertIn('"outfit_style": "用户指定"', prompt)

        payload = {"outfit": "黑丝和吊带裙", "schedule": "下午去喝茶"}
        ok, reason = generator._validate_payload(
            payload,
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝和吊带裙",
        )
        self.assertTrue(ok, reason)

        bad_payload = {"outfit": "白色T恤", "schedule": "下午去喝茶"}
        ok, reason = generator._validate_payload(
            bad_payload,
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝和吊带裙",
        )
        self.assertFalse(ok)
        self.assertIn("黑丝", reason)

        style_only_payload = {
            "outfit_style": "黑丝吊带裙",
            "outfit": "白色T恤",
            "schedule": "下午去喝茶",
        }
        ok, reason = generator._validate_payload(
            style_only_payload,
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝和吊带裙",
        )
        self.assertFalse(ok)
        self.assertIn("穿搭缺少", reason)

        data = generator._to_schedule_data(
            payload,
            "2026-05-24",
            _ctx(),
            manual_extra="穿黑丝和吊带裙",
        )
        self.assertEqual(data.outfit_style, "用户指定")

        data = generator._to_schedule_data(
            dict(payload, outfit_style="甜酷混搭风"),
            "2026-05-24",
            _ctx(),
            manual_extra="穿黑丝和吊带裙",
        )
        self.assertEqual(data.outfit_style, "用户指定")

    def test_manual_extra_supports_negative_constraints(self):
        generator, _ = self._generator()
        ok, reason = generator._validate_payload(
            {"outfit": "居家睡裙", "schedule": "不出门，在家看书"},
            _ctx(),
            enforce_style=False,
            manual_extra="不要出门",
        )
        self.assertTrue(ok, reason)

        ok, reason = generator._validate_payload(
            {"outfit": "休闲装", "schedule": "下午出门散步"},
            _ctx(),
            enforce_style=False,
            manual_extra="不要出门",
        )
        self.assertFalse(ok)
        self.assertIn("避免", reason)

        ok, reason = generator._validate_payload(
            {"outfit": "居家睡裙", "schedule": "今天不要再出门，在家看书"},
            _ctx(),
            enforce_style=False,
            manual_extra="不要再出门",
        )
        self.assertTrue(ok, reason)

    def test_manual_extra_splits_mixed_outfit_and_schedule_terms(self):
        generator, _ = self._generator()
        ok, reason = generator._validate_payload(
            {"outfit": "黑丝和吊带裙", "schedule": "下午茶"},
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝和吊带裙去下午茶",
        )
        self.assertTrue(ok, reason)

    def test_manual_extra_splits_reversed_activity_and_outfit_terms(self):
        generator, _ = self._generator()
        ok, reason = generator._validate_payload(
            {"outfit": "黑丝和吊带裙", "schedule": "下午茶"},
            _ctx(),
            enforce_style=False,
            manual_extra="去下午茶穿黑丝和吊带裙",
        )
        self.assertTrue(ok, reason)

        ok, reason = generator._validate_payload(
            {"outfit": "吊带裙", "schedule": "下午茶"},
            _ctx(),
            enforce_style=False,
            manual_extra="去下午茶穿黑丝和吊带裙",
        )
        self.assertFalse(ok)
        self.assertIn("黑丝", reason)

    def test_manual_extra_matches_compact_outfit_terms(self):
        generator, _ = self._generator()
        ok, reason = generator._validate_payload(
            {"outfit": "黑丝搭配吊带裙", "schedule": "下午在家看书"},
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝吊带裙",
        )
        self.assertTrue(ok, reason)

        ok, reason = generator._validate_payload(
            {"outfit": "黑丝搭配短裙", "schedule": "下午在家看书"},
            _ctx(),
            enforce_style=False,
            manual_extra="穿黑丝吊带裙",
        )
        self.assertFalse(ok)
        self.assertIn("吊带裙", reason)

    def test_manual_extra_matches_activity_keyword_not_exact_phrase(self):
        generator, _ = self._generator()
        ok, reason = generator._validate_payload(
            {"outfit": "休闲裙", "schedule": "15:00 去店里享用下午茶"},
            _ctx(),
            enforce_style=False,
            manual_extra="喝下午茶",
        )
        self.assertTrue(ok, reason)

    def test_normal_generation_keeps_random_style_validation(self):
        generator, _ = self._generator()
        payload = {
            "outfit_style": "甜酷混搭风",
            "outfit": "风格：甜酷混搭风\n黑色短外套搭配短裙。",
            "schedule": "09:30 出门散步",
        }
        ok, reason = generator._validate_payload(payload, _ctx())
        self.assertTrue(ok, reason)

        bad_payload = dict(payload, outfit_style="法式优雅风")
        ok, reason = generator._validate_payload(bad_payload, _ctx())
        self.assertFalse(ok)
        self.assertIn("outfit_style", reason)

    def test_select_current_activity_uses_latest_started_entry(self):
        schedule = (
            "☀️ 上午\n"
            "- 08:00 起床洗漱\n"
            "- 09:30 出门去咖啡店看书\n"
            "- 14:00 去逛街\n"
        )
        now = datetime.datetime(2026, 5, 24, 9, 38)
        self.assertEqual(
            select_current_activity(schedule, now=now),
            "09:30 出门去咖啡店看书",
        )

    def test_select_current_activity_accepts_common_chinese_prefixes(self):
        schedule = (
            "☀️ 上午 8:00 起床洗漱\n"
            "🌤 午后 12点30 出门喝柠檬茶\n"
            "晚上 20:00 回家整理照片\n"
        )
        now = datetime.datetime(2026, 5, 24, 12, 45)
        self.assertEqual(
            select_current_activity(schedule, now=now),
            "12:30 出门喝柠檬茶",
        )

    def test_select_current_activity_accepts_half_hour_cn_time(self):
        schedule = (
            "上午 9点半 出门去咖啡店看书\n"
            "晚上 20点 回家整理照片\n"
        )
        now = datetime.datetime(2026, 5, 24, 9, 45)
        self.assertEqual(
            select_current_activity(schedule, now=now),
            "09:30 出门去咖啡店看书",
        )

    def test_select_current_activity_accepts_numbered_items(self):
        schedule = (
            "1. 08:00 起床洗漱\n"
            "2、09:30 出门去咖啡店看书\n"
        )
        now = datetime.datetime(2026, 5, 24, 9, 45)
        self.assertEqual(
            select_current_activity(schedule, now=now),
            "09:30 出门去咖啡店看书",
        )

    def test_select_current_activity_wraps_to_previous_day_when_needed(self):
        schedule = (
            "- 08:00 起床洗漱\n"
            "- 23:00 窝在被子里看电影\n"
        )
        now = datetime.datetime(2026, 5, 25, 2, 10)
        self.assertEqual(
            select_current_activity(schedule, now=now, wrap_previous_day=True),
            "23:00 窝在被子里看电影",
        )
        self.assertEqual(
            select_current_activity(schedule, now=now, wrap_previous_day=False),
            "08:00 起床洗漱",
        )

    def test_character_state_injection_includes_current_activity(self):
        schedule = (
            "- 08:00 起床洗漱\n"
            "- 09:30 出门去咖啡店看书\n"
            "- 14:00 去逛街\n"
        )
        inject_text = build_character_state_injection(
            "黑丝和吊带裙",
            schedule,
            now=datetime.datetime(2026, 5, 24, 9, 38),
            business_now=datetime.datetime(2026, 5, 24, 9, 38),
        )

        self.assertIn("当前状态: 09:30 出门去咖啡店看书", inject_text)
        self.assertIn("今日日程: " + schedule, inject_text)
        self.assertIn("必须以 <character_state> 为准", inject_text)

    def test_character_state_injection_falls_back_for_plain_schedule(self):
        inject_text = build_character_state_injection(
            "居家裙",
            "上午整理房间，下午在家看书。",
            now=datetime.datetime(2026, 5, 24, 15, 0),
        )

        self.assertIn("当前状态: 未解析到具体时间点", inject_text)
        self.assertIn("今日日程: 上午整理房间，下午在家看书。", inject_text)

    async def test_manual_extra_repairs_when_output_ignores_requirement(self):
        generator, provider = self._generator(
            [
                '{"outfit_style":"用户指定","outfit":"白色T恤","schedule":"下午去喝茶"}',
                (
                    '{"outfit_style":"用户指定",'
                    '"outfit":"黑丝和吊带裙",'
                    '"schedule":"下午穿黑丝和吊带裙去喝茶"}'
                ),
            ]
        )
        data = await generator.generate_schedule(
            datetime.datetime(2026, 5, 24),
            None,
            extra="穿黑丝和吊带裙",
        )

        self.assertEqual(data.status, "ok")
        self.assertEqual(data.outfit_style, "用户指定")
        self.assertIn("黑丝", data.outfit)
        self.assertEqual(len(provider.prompts), 2)

    async def test_empty_completion_retries_once_then_succeeds(self):
        generator, provider = self._generator(
            [
                "",
                (
                    '{"outfit_style":"甜酷混搭风",'
                    '"outfit":"风格：甜酷混搭风\\n黑色短外套搭配短裙。",'
                    '"schedule":"09:30 出门散步"}'
                ),
            ]
        )
        data = await generator.generate_schedule(datetime.datetime(2026, 5, 24), None)

        self.assertEqual(data.status, "ok")
        self.assertEqual(len(provider.prompts), 2)

    async def test_empty_completion_returns_failed_schedule(self):
        generator, provider = self._generator(["", ""])
        data = await generator.generate_schedule(datetime.datetime(2026, 5, 24), None)

        self.assertEqual(data.status, "failed")
        self.assertEqual(len(provider.prompts), 2)


if __name__ == "__main__":
    unittest.main()
