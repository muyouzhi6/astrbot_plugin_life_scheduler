import datetime
import json
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
    """Persist normalized outfit plans independently from daily schedules."""

    def __init__(self, json_path: Path):
        self._path = json_path
        self._entries: list[dict[str, str]] = []
        self.load()

    def load(self) -> None:
        """Load wardrobe entries, tolerating missing or malformed files."""
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

        entries: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description", "")).strip()
            if not description:
                continue
            entries.append(
                {
                    "id": str(item.get("id", len(entries) + 1)),
                    "created_at": str(item.get("created_at", "")),
                    "description": description,
                    "note": str(item.get("note", "")).strip(),
                }
            )
        self._entries = entries

    def add(self, description: str, *, note: str = "") -> dict[str, str]:
        """Add one normalized outfit plan and persist it atomically.

        Args:
            description: Detailed outfit description produced by the vision model.
            note: Optional user text sent with the image.

        Returns:
            The persisted wardrobe entry.

        Raises:
            ValueError: If the description is empty.
        """
        description = str(description or "").strip()
        if not description:
            raise ValueError("Wardrobe description cannot be empty")

        entry = {
            "id": datetime.datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "description": description,
            "note": str(note or "").strip(),
        }
        self._entries.append(entry)
        self.save()
        return dict(entry)

    def all(self) -> list[dict[str, str]]:
        """Return a shallow copy of all wardrobe entries."""
        return [dict(entry) for entry in self._entries]

    def display_entries(self, *, limit: int = 20) -> list[dict[str, str]]:
        """Return entries in the same newest-first order shown to users.

        Args:
            limit: Maximum number of recent entries to expose.

        Returns:
            Copies of recent entries ordered for display, newest first.
        """
        return [dict(entry) for entry in reversed(self._entries[-limit:])]

    def find_by_number(self, number: int, *, limit: int = 20) -> dict[str, str] | None:
        """Find an entry by its one-based number in the display list.

        Args:
            number: One-based number shown in the wardrobe image.
            limit: Number of recent entries included in the display list.

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
        """Find the newest entry whose id or description contains a query."""
        query = str(query or "").strip().casefold()
        if not query:
            return None
        for entry in reversed(self._entries):
            if (
                query in entry["id"].casefold()
                or query in entry["description"].casefold()
            ):
                return dict(entry)
        return None

    def replace(
        self, entry_id: str, description: str, *, note: str = ""
    ) -> dict[str, str]:
        """Replace one existing entry and persist the result.

        Args:
            entry_id: Stable wardrobe entry identifier.
            description: New normalized outfit description.
            note: Optional replacement note.

        Returns:
            The updated entry.

        Raises:
            KeyError: If the entry does not exist.
            ValueError: If the description is empty.
        """
        description = str(description or "").strip()
        if not description:
            raise ValueError("Wardrobe description cannot be empty")
        for entry in self._entries:
            if entry["id"] != str(entry_id):
                continue
            entry["description"] = description
            entry["note"] = str(note or "").strip()
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
        """Format recent wardrobe entries for schedule generation."""
        if not self._entries:
            return "（衣柜为空）"

        lines: list[str] = []
        for index, entry in enumerate(self.display_entries(limit=limit), start=1):
            note = f"；用户备注：{entry['note']}" if entry.get("note") else ""
            lines.append(f"{index}. {entry['description']}{note}")
        text = "\n".join(lines)
        return text[:max_chars]

    def save(self) -> None:
        """Persist entries with an atomic replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._path)
