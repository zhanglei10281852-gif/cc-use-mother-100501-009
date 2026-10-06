"""统一的逐时时间桶与会计月份工具。

系统内部所有时间都按整小时桶（``YYYY-MM-DDTHH``）表达，会计月份为
``YYYY-MM``。只依赖标准库，保证复算结果与运行时区无关：时间桶是显式传入的
业务数据，而不是在计算瞬间读取系统时钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta

HOUR_FORMAT = "%Y-%m-%dT%H"
MONTH_FORMAT = "%Y-%m"


def parse_hour(value: str) -> datetime:
    """解析小时桶，非法格式直接失败，避免脏口径进入台账。"""
    try:
        return datetime.strptime(value, HOUR_FORMAT)
    except ValueError as exc:  # pragma: no cover - 错误消息透传
        raise ValueError(f"小时桶格式应为 YYYY-MM-DDTHH: {value!r}") from exc


def format_hour(value: datetime) -> str:
    return value.strftime(HOUR_FORMAT)


def add_hours(value: str, hours: int) -> str:
    return format_hour(parse_hour(value) + timedelta(hours=hours))


def month_of(hour: str) -> str:
    parse_hour(hour)
    return hour[:7]


def hours_between(start: str, end: str) -> list[str]:
    """返回 ``[start, end)`` 的全部小时桶。"""
    current = parse_hour(start)
    finish = parse_hour(end)
    if finish < current:
        raise ValueError("结束小时不能早于开始小时")
    result: list[str] = []
    while current < finish:
        result.append(format_hour(current))
        current += timedelta(hours=1)
    return result


def current_month(now: datetime | None = None) -> str:
    moment = now or datetime.utcnow()
    return moment.strftime(MONTH_FORMAT)
