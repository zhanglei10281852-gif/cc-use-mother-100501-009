"""统一的时间口径：所有时段均为北京时间（UTC+8）整点小时。

取水许可、生态下泄要求、逐时计量分别按不同口径维护是月末才发现透支的根因，
因此全系统只承认一种口径——``[start, end)`` 左闭右开的整点小时区间，
并以 ``YYYY-MM-DDTHH:00+08:00`` 字符串作为外部表示。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

BEIJING = timezone(timedelta(hours=8), name="CST")
HOUR = timedelta(hours=1)


class HourInterval:
    """左闭右开的整点小时区间，提供系统内唯一的时段运算。"""

    __slots__ = ("start", "end")

    def __init__(self, start: datetime, end: datetime) -> None:
        self.start = _floor_hour(_as_aware(start))
        self.end = _floor_hour(_as_aware(end))
        if self.end <= self.start:
            raise ValueError(f"时段结束必须晚于开始: {self.start} ~ {self.end}")

    @classmethod
    def parse(cls, text: str) -> "HourInterval":
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间 {text!r}") from exc
        if value.tzinfo is None:
            raise ValueError(
                f"时间必须显式声明时区偏移（统一口径 +08:00）: {text!r}"
            )
        value = _as_aware(value)
        if value.minute or value.second or value.microsecond:
            raise ValueError(f"时间必须为整点: {text!r}")
        # 单时间戳表示该小时 [t, t+1h)
        return cls(value, value + HOUR)

    @classmethod
    def span(cls, start_text: str, end_text: str) -> "HourInterval":
        start = cls.parse(start_text).start
        end = cls.parse(end_text).start
        return cls(start, end)

    @property
    def hours(self) -> int:
        return int((self.end - self.start) // HOUR)

    def each_hour(self) -> Iterator[datetime]:
        current = self.start
        while current < self.end:
            yield current
            current += HOUR

    def overlap(self, other: "HourInterval") -> "HourInterval | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start < end:
            return HourInterval(start, end)
        return None

    def contains(self, moment: datetime) -> bool:
        return self.start <= _as_aware(moment) < self.end

    def split_hours(self) -> list["HourInterval"]:
        return [HourInterval(moment, moment + HOUR) for moment in self.each_hour()]

    @property
    def key(self) -> str:
        """小时桶键，用于逐时计量归集。"""
        return self.start.strftime("%Y-%m-%dT%H:00")

    def start_text(self) -> str:
        return iso(self.start)

    def end_text(self) -> str:
        return iso(self.end)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HourInterval):
            return NotImplemented
        return self.start == other.start and self.end == other.end

    def __repr__(self) -> str:
        return f"HourInterval({self.key}~+{self.hours}h)"


def iso(value: datetime) -> str:
    """统一外部时间表示（带 +08:00 偏移，不使用 Z/无时区时间）。"""
    return _floor_hour(_as_aware(value)).strftime("%Y-%m-%dT%H:00+08:00")


def now() -> datetime:
    return datetime.now(BEIJING)


def month_range(year: int, month: int) -> HourInterval:
    start = datetime(year, month, 1, tzinfo=BEIJING)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=BEIJING)
    else:
        end = datetime(year, month + 1, 1, tzinfo=BEIJING)
    return HourInterval(start, end)


def month_key(value: datetime) -> str:
    return _as_aware(value).strftime("%Y-%m")


def merge_intervals(intervals: Iterable[HourInterval]) -> list[HourInterval]:
    ordered = sorted(intervals, key=lambda item: item.start)
    merged: list[HourInterval] = []
    for item in ordered:
        if merged and item.start <= merged[-1].end:
            merged[-1] = HourInterval(merged[-1].start, max(merged[-1].end, item.end))
        else:
            merged.append(item)
    return merged


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)
