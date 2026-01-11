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
    outfit: str = ""
    schedule: str = ""
    status: ScheduleStatus = "ok"

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleData":
        """允许未来字段扩展"""
        return cls(
            date=data["date"],
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
