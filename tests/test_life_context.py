import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "life_scheduler_plugin_test"


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = debug
    warning = debug
    error = debug


class _Filter:
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    def __getattr__(self, name):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        return decorator_factory


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api_all = types.ModuleType("astrbot.api.all")
    api_all.Context = object
    api_all.Star = type("Star", (), {})
    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = object
    api_event.filter = _Filter()
    api_components = types.ModuleType("astrbot.api.message_components")

    class Image:
        def __init__(self, file=""):
            self.file = file
            self.url = ""

        async def convert_to_file_path(self):
            return self.file

    api_components.Image = Image
    api_components.Reply = type("Reply", (), {})
    config = types.ModuleType("astrbot.core.config.astrbot_config")
    config.AstrBotConfig = dict
    entities = types.ModuleType("astrbot.core.provider.entities")
    entities.ProviderRequest = object
    star_tools = types.ModuleType("astrbot.core.star.star_tools")
    star_tools.StarTools = types.SimpleNamespace(
        get_data_dir=lambda: Path(tempfile.gettempdir())
    )
    command = types.ModuleType("astrbot.core.star.filter.command")
    command.GreedyStr = str
    quoted = types.ModuleType("astrbot.core.utils.quoted_message_parser")

    class QuotedMessageExtractor:
        def __init__(self, event):
            self.event = event

            async def get_msg(reply_id):
                return await event.bot.api.call_action("get_msg", message_id=reply_id)

            self._client = types.SimpleNamespace(
                call=event.bot.api.call_action, get_msg=get_msg
            )
            self._reply_parser = types.SimpleNamespace(
                extract_image_refs_from_reply_component=lambda reply: []
            )
            self._payload_parser = types.SimpleNamespace(
                parse_get_msg_payload=lambda refs: {
                    "image_refs": refs,
                    "forward_ids": [],
                }
            )
            self._settings = types.SimpleNamespace(max_forward_fetch=32)

        async def _fetch_quoted_content(self, reply, *, fetch_remote):
            refs = await self.event.bot.api.call_action("get_msg", message_id=reply.id)
            return types.SimpleNamespace(
                embedded_image_refs=[], direct_image_refs=refs, forward_image_refs=[]
            )

        async def images(self, reply):
            content = await self._fetch_quoted_content(reply, fetch_remote=True)
            return [
                await self.event.bot.api.call_action("get_image", {"file": ref})
                for ref in content.direct_image_refs
            ]

    async def extract_quoted_message_images(event, reply):
        if not hasattr(event, "bot"):
            return []
        return await QuotedMessageExtractor(event).images(reply)

    quoted.extract_quoted_message_images = extract_quoted_message_images
    extractor = types.ModuleType("astrbot.core.utils.quoted_message.extractor")
    extractor.QuotedMessageExtractor = QuotedMessageExtractor
    image_refs = types.ModuleType("astrbot.core.utils.quoted_message.image_refs")
    image_refs.normalize_image_ref = lambda ref: (
        ref if ref.startswith(("http://", "https://", "base64://")) else None
    )
    image_refs.get_existing_local_path = lambda ref: None
    image_resolver = types.ModuleType(
        "astrbot.core.utils.quoted_message.image_resolver"
    )
    image_resolver._build_image_resolve_actions = lambda event, ref: [
        ("get_image", {"file": ref})
    ]
    context = types.ModuleType("astrbot.core.star.context")
    context.Context = object
    schedule = types.ModuleType(f"{PACKAGE}.core.schedule")
    schedule.LifeScheduler = type("LifeScheduler", (), {})
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.all": api_all,
            "astrbot.api.event": api_event,
            "astrbot.api.message_components": api_components,
            "astrbot.core.config.astrbot_config": config,
            "astrbot.core.provider.entities": entities,
            "astrbot.core.star.star_tools": star_tools,
            "astrbot.core.star.filter.command": command,
            "astrbot.core.utils.quoted_message_parser": quoted,
            "astrbot.core.utils.quoted_message.extractor": extractor,
            "astrbot.core.utils.quoted_message.image_refs": image_refs,
            "astrbot.core.utils.quoted_message.image_resolver": image_resolver,
            "astrbot.core.star.context": context,
            f"{PACKAGE}.core.schedule": schedule,
        }
    )
    return schedule


def _load_main():
    _install_stubs()
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    core_package = types.ModuleType(f"{PACKAGE}.core")
    core_package.__path__ = [str(ROOT / "core")]
    core_package.schedule = sys.modules[f"{PACKAGE}.core.schedule"]
    sys.modules[f"{PACKAGE}.core"] = core_package
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LifeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_reports_current_activity_without_selecting_future_entries(
        self,
    ):
        module = _load_main()
        from core.data import ScheduleData, ScheduleDataManager

        schedule = "08:00 喝咖啡\n09:00 写代码\n14:00 在窗边看书\n22:30 洗漱睡觉"
        cases = [
            ("08:15", "08:00 喝咖啡"),
            ("09:00", "09:00 写代码"),
            ("13:59", "09:00 写代码"),
            ("14:30", "14:00 在窗边看书"),
            ("22:30", "22:30 洗漱睡觉"),
            ("02:15", "22:30 洗漱睡觉"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(module.LifeSchedulerPlugin)
            plugin.config = {"schedule_time": "07:00"}
            plugin.data_mgr = ScheduleDataManager(Path(temp_dir) / "schedule.json")
            plugin.data_mgr.set(
                ScheduleData(date="2026-01-01", outfit="女仆服", schedule=schedule)
            )
            for clock, expected in cases:
                with self.subTest(clock=clock):
                    business_now = datetime.fromisoformat("2026-01-01T" + clock)
                    with patch.object(
                        module, "resolve_business_now", return_value=business_now
                    ):
                        result = await plugin.get_life_context(allow_generate=False)
                    self.assertEqual(result["current_time"], clock)
                    self.assertEqual(result["current_activity"], expected)
                    self.assertEqual(result["schedule"], schedule)
                    self.assertEqual(result["outfit"], "女仆服")

    async def test_context_keeps_untimed_schedule_without_inventing_an_activity(self):
        module = _load_main()
        from core.data import ScheduleData, ScheduleDataManager

        business_now = datetime(2026, 1, 1, 14, 30)
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(module.LifeSchedulerPlugin)
            plugin.config = {"schedule_time": "07:00"}
            plugin.data_mgr = ScheduleDataManager(Path(temp_dir) / "schedule.json")
            plugin.data_mgr.set(
                ScheduleData(
                    date="2026-01-01", outfit="水手服", schedule="下午去咖啡店"
                )
            )
            with patch.object(
                module, "resolve_business_now", return_value=business_now
            ):
                result = await plugin.get_life_context(allow_generate=False)
        self.assertEqual(result["current_activity"], "")
        self.assertEqual(result["current_time"], "14:30")
        self.assertEqual(result["schedule"], "下午去咖啡店")

    async def test_read_only_context_uses_cache_and_skips_generator(self):
        module = _load_main()
        from core.data import ScheduleData, ScheduleDataManager
        from core.utils import resolve_business_now

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = ScheduleDataManager(Path(temp_dir) / "schedule.json")
            config = {"schedule_time": "00:00"}
            today = resolve_business_now(config["schedule_time"])
            manager.set(
                ScheduleData(
                    date=today.strftime("%Y-%m-%d"),
                    outfit="白衬衫搭配蓝色半裙",
                    schedule="上午去咖啡店",
                )
            )
            plugin = object.__new__(module.LifeSchedulerPlugin)
            plugin.config = config
            plugin.data_mgr = manager

            class _Generator:
                async def generate_schedule(self, *args, **kwargs):
                    raise AssertionError("read-only context must not generate")

            plugin.generator = _Generator()
            result = await plugin.get_life_context(allow_generate=False)

        self.assertEqual(result["outfit"], "白衬衫搭配蓝色半裙")
        self.assertEqual(result["schedule"], "上午去咖啡店")

    async def test_read_only_context_returns_empty_without_cache(self):
        module = _load_main()
        from core.data import ScheduleDataManager

        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = object.__new__(module.LifeSchedulerPlugin)
            plugin.config = {"schedule_time": "00:00"}
            plugin.data_mgr = ScheduleDataManager(Path(temp_dir) / "schedule.json")
            calls = []

            class _Generator:
                async def generate_schedule(self, *args, **kwargs):
                    calls.append((args, kwargs))
                    return None

            plugin.generator = _Generator()
            result = await plugin.get_life_context(allow_generate=False)

        self.assertEqual(result, {})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
