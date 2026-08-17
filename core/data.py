import datetime
import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Union

# =========================
# 类型定义
# =========================

ScheduleStatus = Literal["ok", "failed"]

DateLike = Union[  # noqa: UP007
    datetime.datetime,
    datetime.date,
    int,  # timestamp
    float,  # timestamp
]


# =========================
# 工具函数（时间归一化）
# =========================


def to_date_str(value: DateLike) -> str:
    """统一将时间输入转为 yyyy-mm-dd 字符串"""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, int | float):
        return datetime.datetime.fromtimestamp(value).date().isoformat()
    raise TypeError(f"Unsupported date type: {type(value)}")


# =========================
# 数据结构
# =========================


@dataclass(slots=True)
class ScheduleData:
    """单日数据（date 只作为内部 key，不对外暴露格式责任）"""

    date: str  # yyyy-mm-dd
    outfit_style: str = ""
    outfit: str = ""
    schedule: str = ""
    status: ScheduleStatus = "ok"

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleData":
        """允许未来字段扩展"""
        return cls(
            date=data["date"],
            outfit_style=data.get("outfit_style", ""),
            outfit=data.get("outfit", ""),
            schedule=data.get("schedule", ""),
            status=data.get("status", "ok"),
        )


# =========================
# 数据管理器（纯存取）
# =========================


class ScheduleDataManager:
    """
    纯数据层：
    - 内存存取
    - JSON 持久化
    """

    def __init__(self, json_path: Path):
        self._path = json_path
        self._data: dict[str, ScheduleData] = {}

        self.load()

    # ---------- 基础 CRUD ----------

    def has(self, date: DateLike) -> bool:
        return to_date_str(date) in self._data

    def get(self, date: DateLike) -> ScheduleData | None:
        return self._data.get(to_date_str(date))

    def set(self, data: ScheduleData) -> None:
        self._data[data.date] = data
        self.save()

    def remove(self, date: DateLike) -> None:
        if self._data.pop(to_date_str(date), None):
            self.save()

    def all(self) -> dict[str, ScheduleData]:
        """返回副本，防止外部污染"""
        return dict(self._data)

    # ---------- JSON 持久化 ----------

    def load(self) -> None:
        """从 JSON 加载（文件不存在则视为空）"""
        if not self._path.exists():
            self._data.clear()
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # 文件损坏时直接清空，交给上层兜底
            self._data.clear()
            return

        data: dict[str, ScheduleData] = {}
        for date_str, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                data[date_str] = ScheduleData.from_dict(item)
            except Exception:
                continue

        self._data = data

    def save(self) -> None:
        """保存为 JSON（原子写）"""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self._path.with_suffix(".tmp")
        payload = {date: asdict(data) for date, data in self._data.items()}

        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    # ---------- 工具方法 ----------

    def clear(self, *, save: bool = True) -> None:
        """清空所有数据"""
        self._data.clear()
        if save:
            self.save()


class WardrobeDataManager:
    """Persist normalized outfit plans in the plugin configuration."""

    def __init__(self, json_path: Path, config: dict | None = None):
        self._path = json_path
        self._config = config
        self._entries: list[dict[str, str]] = []
        self.load()

    def load(self) -> None:
        """Load wardrobe entries and migrate legacy sources when configured."""
        if self._config is not None:
            configured = self._normalize_entries(self._config.get("wardrobe", []))
            migration_version = int(
                self._config.get("wardrobe_migration_version", 0) or 0
            )
            config_changed = migration_version < 2
            if migration_version < 1:
                legacy_descriptions: list[str] = []
                legacy_file_valid = True
                if self._path.exists():
                    try:
                        raw = json.loads(self._path.read_text(encoding="utf-8"))
                        if not isinstance(raw, list):
                            raise ValueError("legacy wardrobe root is not a list")
                        for item in reversed(raw):
                            if not isinstance(item, dict):
                                continue
                            description = str(item.get("description", "")).strip()
                            if description:
                                legacy_descriptions.append(description)
                    except Exception as exc:
                        legacy_file_valid = False
                        logging.getLogger("astrbot").error(
                            "Legacy wardrobe migration skipped for malformed file: %s",
                            exc,
                        )

                legacy_styles = self._normalize_descriptions(
                    self._config.get("pool", {}).get("outfit_styles", [])
                )
                migrated_styles = [
                    (
                        f"整体风格：{style}；服装：未明确；鞋袜：未明确；"
                        "配饰：未明确；发型：未明确；妆容：未明确。"
                    )
                    for style in legacy_styles
                ]
                configured = self._normalize_entries(
                    legacy_descriptions + configured + migrated_styles
                )
                pool = self._config.get("pool")
                if isinstance(pool, dict):
                    pool["outfit_styles"] = []
                if legacy_file_valid:
                    self._config["wardrobe_migration_version"] = 1

                if legacy_file_valid and self._path.exists():
                    tmp_path = self._path.with_suffix(".tmp")
                    tmp_path.write_text("[]\n", encoding="utf-8")
                    tmp_path.replace(self._path)

            self._entries = configured
            self._config["wardrobe_migration_version"] = 2
            serialized = self._serialize_entries()
            if self._config.get("wardrobe") != serialized:
                self._config["wardrobe"] = serialized
                config_changed = True
            if config_changed:
                save_config = getattr(self._config, "save_config", None)
                if callable(save_config):
                    save_config()
            return

        if not self._path.exists():
            self._entries = []
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._entries = []
            return

        if not isinstance(raw, list):
            self._entries = []
            return

        self._entries = self._normalize_entries(raw)

    @staticmethod
    def _normalize_descriptions(values: object) -> list[str]:
        """Normalize a configuration list while preserving its visible order.

        Args:
            values: Candidate wardrobe descriptions.

        Returns:
            Non-empty unique descriptions in input order.
        """
        if not isinstance(values, list):
            return []
        descriptions: list[str] = []
        seen: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                value = value.get("description", "")
            description = str(value or "").strip()
            canonical = description.casefold()
            if not description or canonical in seen:
                continue
            seen.add(canonical)
            descriptions.append(description)
        return descriptions

    @staticmethod
    def _build_entry(description: str, entry_id: str = "") -> dict[str, str]:
        """Build the runtime view of one configured wardrobe entry.

        Args:
            description: Normalized outfit-only description.

        Returns:
            A dictionary compatible with existing wardrobe callers.
        """
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]
        return {
            "id": entry_id or f"wardrobe-{digest}",
            "created_at": "",
            "description": description,
            "note": "",
        }

    @classmethod
    def _normalize_entries(cls, values: object) -> list[dict[str, str]]:
        """Normalize visible descriptions while preserving stable entry IDs.

        Args:
            values: Legacy strings or template-list entry dictionaries.

        Returns:
            Unique wardrobe entries in configured order.
        """
        if not isinstance(values, list):
            return []

        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in values:
            source_id = ""
            if isinstance(value, dict):
                source_id = str(value.get("id", "")).strip()
                value = value.get("description", "")
            description = str(value or "").strip()
            canonical = description.casefold()
            if not description or canonical in seen:
                continue
            seen.add(canonical)
            if not re.fullmatch(r"wardrobe-[A-Za-z0-9_-]+", source_id):
                source_id = ""
            entries.append(cls._build_entry(description, source_id))
        return entries

    def _serialize_entries(self) -> list[dict[str, str]]:
        """Serialize entries to the template-list configuration shape."""
        return [
            {
                "__template_key": "outfit",
                "id": entry["id"],
                "description": entry["description"],
            }
            for entry in self._entries
        ]

    def add(self, description: str, *, note: str = "") -> dict[str, str]:
        """Add one normalized outfit plan and persist it atomically.

        Args:
            description: Detailed outfit description produced by the vision model.
            note: Deprecated source text. It is intentionally not persisted.

        Returns:
            The persisted wardrobe entry.

        Raises:
            ValueError: If the description is empty.
        """
        description = str(description or "").strip()
        if not description:
            raise ValueError("Wardrobe description cannot be empty")

        del note
        canonical = description.casefold()
        for entry in self._entries:
            if entry["description"].casefold() == canonical:
                return dict(entry)

        entry = self._build_entry(description, f"wardrobe-{uuid.uuid4().hex}")
        self._entries.append(entry)
        self.save()
        return dict(entry)

    def all(self) -> list[dict[str, str]]:
        """Return a shallow copy of all wardrobe entries."""
        return [dict(entry) for entry in self._entries]

    def display_entries(self, *, limit: int | None = None) -> list[dict[str, str]]:
        """Return entries in stable configured order.

        Args:
            limit: Optional maximum number of entries to expose.

        Returns:
            Copies ordered exactly as their stable user-facing numbering.
        """
        entries = self._entries if limit is None else self._entries[:limit]
        return [dict(entry) for entry in entries]

    def find_by_number(
        self, number: int, *, limit: int | None = None
    ) -> dict[str, str] | None:
        """Find an entry by its one-based number in the display list.

        Args:
            number: One-based number shown in the wardrobe image.
            limit: Optional number of entries included in the display list.

        Returns:
            The matching entry, or None when the number is out of range.
        """
        if number < 1:
            return None
        entries = self.display_entries(limit=limit)
        if number > len(entries):
            return None
        return entries[number - 1]

    def find(self, query: str) -> dict[str, str] | None:
        """Find the first configured entry matching an id or description query."""
        query = str(query or "").strip().casefold()
        if not query:
            return None
        for entry in self._entries:
            if (
                query in entry["id"].casefold()
                or query in entry["description"].casefold()
            ):
                return dict(entry)
        return None

    def find_for_user_query(
        self, query: str, *, limit: int | None = None
    ) -> dict[str, str] | None:
        """Resolve a user-facing wardrobe number or a stable query.

        Args:
            query: Display number such as ``穿搭2``/``2``, a stable entry ID,
                or a description keyword.
            limit: Optional number of entries exposed as display numbers.

        Returns:
            The matching wardrobe entry, or ``None`` when no entry matches.
        """
        query_text = str(query or "").strip()
        query_match = re.fullmatch(r"(穿搭\s*)?(\d+)", query_text)
        if query_match:
            number = int(query_match.group(2))
            numbered = self.find_by_number(number, limit=limit)
            if numbered is not None or query_match.group(1):
                return numbered
        return self.find(query_text)

    def replace(
        self, entry_id: str, description: str, *, note: str = ""
    ) -> dict[str, str]:
        """Replace one existing entry and persist the result.

        Args:
            entry_id: Stable wardrobe entry identifier.
            description: New normalized outfit description.
            note: Deprecated source text. It is intentionally not persisted.

        Returns:
            The updated entry.

        Raises:
            KeyError: If the entry does not exist.
            ValueError: If the description is empty.
        """
        description = str(description or "").strip()
        if not description:
            raise ValueError("Wardrobe description cannot be empty")
        del note
        canonical = description.casefold()
        for entry in self._entries:
            if (
                entry["id"] != str(entry_id)
                and entry["description"].casefold() == canonical
            ):
                raise ValueError("Wardrobe description already exists")
        for entry in self._entries:
            if entry["id"] != str(entry_id):
                continue
            entry["description"] = description
            self.save()
            return dict(entry)
        raise KeyError(entry_id)

    def remove(self, entry_id: str) -> dict[str, str]:
        """Remove one entry by stable identifier and persist the result."""
        for index, entry in enumerate(self._entries):
            if entry["id"] != str(entry_id):
                continue
            removed = self._entries.pop(index)
            self.save()
            return dict(removed)
        raise KeyError(entry_id)

    def for_prompt(self, *, limit: int = 20, max_chars: int = 12000) -> str:
        """Format wardrobe entries without leaking source notes."""
        if not self._entries:
            return "（衣柜为空）"

        lines: list[str] = []
        for index, entry in enumerate(self.display_entries(limit=limit), start=1):
            lines.append(f"{index}. {entry['description']}")
        text = "\n".join(lines)
        return text[:max_chars]

    def save(self) -> None:
        """Persist entries to configuration or the legacy JSON fallback."""
        if self._config is not None:
            self._config["wardrobe"] = self._serialize_entries()
            save_config = getattr(self._config, "save_config", None)
            if callable(save_config):
                save_config()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
