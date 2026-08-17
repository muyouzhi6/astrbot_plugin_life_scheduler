import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


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
    api_components.Image = type("Image", (), {})
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

    async def extract_quoted_message_images(*args, **kwargs):
        return []

    quoted.extract_quoted_message_images = extract_quoted_message_images
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
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LifeContextTests(unittest.IsolatedAsyncioTestCase):
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
