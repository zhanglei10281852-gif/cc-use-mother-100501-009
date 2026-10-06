"""持久化哈希链：重载校验、篡改检测与跨进程复算一致性。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from water_compliance import ComplianceService, DocumentStore, EventStore
from water_compliance.ledger import EventChainBroken


class PersistenceTests(unittest.TestCase):
    def test_reload_reproduces_state_and_recomputation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            svc = ComplianceService(
                EventStore(home / "events.jsonl"),
                DocumentStore(home / "documents.jsonl"),
            )
            svc.register_user("U1", "工业园", "industrial_park")
            svc.register_section("S1", "断面", [("dry", "11-01", "03-31", 500.0)])
            svc.register_point("P1", "取水口", "U1", "东河", "S1")
            svc.add_permit_version(
                point_id="P1", revision="V1",
                valid_from="2026-01-01T00:00+08:00",
                annual_quota_m3=36_000.0,
                purpose_codes=["production"],
                priority_subjects=["工业"],
                seasonal_quotas=[("dry", "11-01", "11-30", 36_000.0)],
            )
            svc.record_meter("m1", "P1", "2026-11-01T00:00+08:00", 70.0)
            before = svc.recompute(
                "P1", "2026-11-01T00:00+08:00", "2026-11-01T01:00+08:00"
            )

            # 重新打开：哈希链校验 + 状态重建
            svc2 = ComplianceService(
                EventStore(home / "events.jsonl"),
                DocumentStore(home / "documents.jsonl"),
            )
            after = svc2.recompute(
                "P1", "2026-11-01T00:00+08:00", "2026-11-01T01:00+08:00"
            )
            self.assertEqual(before, after)

            # 幂等键同样持久化：重复导入仍只算一次
            stored = svc2.record_meter("m1", "P1", "2026-11-01T00:00+08:00", 70.0)
            self.assertEqual(stored.seq, 5)
            self.assertEqual(svc2.store.version(), 5)

    def test_tampered_history_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            # use service to append a valid event
            svc = ComplianceService(store, DocumentStore())
            svc.register_user("U1", "工业园", "industrial_park")
            raw = path.read_text(encoding="utf-8")
            tampered = raw.replace("工业园", "化工园")
            path.write_text(tampered, encoding="utf-8")
            with self.assertRaises(EventChainBroken):
                EventStore(path)

    def test_signed_report_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            kwargs = dict(
                event_path=home / "events.jsonl",
                doc_path=home / "documents.jsonl",
            )
            svc = ComplianceService(
                EventStore(kwargs["event_path"]), DocumentStore(kwargs["doc_path"])
            )
            svc.register_section("S1", "断面", [("dry", "11-01", "11-30", 500.0)])
            svc.register_user("U1", "工业园", "industrial_park")
            svc.register_point("P1", "取水口", "U1", "东河", "S1")
            svc.add_permit_version(
                point_id="P1", revision="V1",
                valid_from="2026-01-01T00:00+08:00",
                annual_quota_m3=36_000.0,
                purpose_codes=["production"],
                priority_subjects=["工业"],
                seasonal_quotas=[("dry", "11-01", "11-30", 36_000.0)],
            )
            svc.record_meter("m1", "P1", "2026-11-01T00:00+08:00", 40.0)
            signed = svc.sign_monthly_report("P1", 2026, 11, "王")

            svc2 = ComplianceService(
                EventStore(kwargs["event_path"]), DocumentStore(kwargs["doc_path"])
            )
            body = svc2.signed_report_body("P1", 2026, 11)
            self.assertEqual(body["summary"], signed["body"]["summary"])
