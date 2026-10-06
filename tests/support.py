"""测试公共夹具：构造最小可用领域环境。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from water_compliance import ComplianceService, DocumentStore, EventStore
from water_compliance.timeutil import BEIJING, iso

_BASE = datetime(2026, 11, 1, 0, 0, tzinfo=BEIJING)


def hour_text(offset: int, base: datetime = _BASE) -> str:
    """从基准时刻起偏移 offset 个整点小时的规范时间串。"""
    return iso(base + timedelta(hours=offset))


def hour_range_text(start_offset: int, hours: int, base: datetime = _BASE) -> tuple[str, str]:
    start = base + timedelta(hours=start_offset)
    return iso(start), iso(start + timedelta(hours=hours))


class DomainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComplianceService(EventStore(), DocumentStore())
        svc = self.service
        svc.register_user("U1", "工业园", "industrial_park")
        svc.register_user("U2", "灌区", "irrigation_district")
        svc.register_section(
            "S1",
            "生态断面",
            [("dry", "11-01", "03-31", 500.0), ("wet", "04-01", "10-31", 300.0)],
        )
        svc.register_point("P1", "工业园取水口", "U1", "东河", "S1")
        svc.register_point(
            "P2", "灌区渠首", "U2", "西河", "S1", routing_lag_hours=2, routing_factor=0.5
        )
        svc.add_permit_version(
            point_id="P1",
            revision="V1",
            valid_from="2026-01-01T00:00+08:00",
            annual_quota_m3=1_440_000.0,
            purpose_codes=["production"],
            priority_subjects=["工业生产"],
            seasonal_quotas=[("dry", "11-01", "11-30", 36_000.0)],  # 50 m³/h
        )
        svc.add_permit_version(
            point_id="P2",
            revision="V1",
            valid_from="2026-01-01T00:00+08:00",
            annual_quota_m3=144_000.0,
            purpose_codes=["agriculture"],
            priority_subjects=["农业灌溉"],
            seasonal_quotas=[("dry", "11-01", "11-30", 7_200.0)],  # 10 m³/h
        )

    def recompute(self, point_id: str = "P1", hours: int = 24, start: str = "2026-11-01T00:00+08:00"):
        from datetime import datetime, timedelta
        from water_compliance.timeutil import BEIJING

        begin = datetime.fromisoformat(start)
        end = begin + timedelta(hours=hours)
        interval_text = end.strftime("%Y-%m-%dT%H:00+08:00")
        return self.service.recompute(point_id, start, interval_text)
