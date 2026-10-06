"""暂估、补报、计量更正、幂等导入与月报不可变测试。"""

from __future__ import annotations

from water_compliance.ledger import IdempotencyConflict
from tests.support import DomainTestCase, hour_text


class MeteringTests(DomainTestCase):
    def test_estimated_reading_requires_source(self) -> None:
        with self.assertRaisesRegex(ValueError, "暂估"):
            self.service.record_meter(
                "e1", "P1", "2026-11-01T00:00+08:00", 50.0, source="estimated"
            )

    def test_duplicate_import_is_idempotent(self) -> None:
        kwargs = dict(
            record_id="dup-1", point_id="P1",
            hour="2026-11-01T00:00+08:00", withdrawal_m3=42.0,
        )
        first = self.service.record_meter(**kwargs)
        second = self.service.record_meter(**kwargs)
        self.assertEqual(first.seq, second.seq)
        self.assertEqual(self.service.store.version(), self.service.store.version())
        result = self.recompute("P1", hours=1)
        self.assertEqual(result["total_withdrawal_m3"], 42.0)

    def test_duplicate_import_with_different_payload_rejected(self) -> None:
        self.service.record_meter(
            "dup-2", "P1", "2026-11-01T00:00+08:00", 42.0
        )
        with self.assertRaises(IdempotencyConflict):
            self.service.record_meter(
                "dup-2", "P1", "2026-11-01T00:00+08:00", 99.0
            )

    def test_amendment_only_changes_difference(self) -> None:
        self.service.record_meter("m1", "P1", "2026-11-01T00:00+08:00", 100.0)
        self.service.amend_meter(
            "a1", "P1", "2026-11-01T00:00+08:00", 130.0, "m1", "校准错误"
        )
        result = self.recompute("P1", hours=1)
        self.assertEqual(result["total_withdrawal_m3"], 130.0)
        row = result["hours"][0]
        self.assertTrue(row["basis"] == ["a1"])
        # 原始事件仍然存在且未被改写
        evidence = self.service.evidence(["m1", "a1"])
        self.assertTrue(all(item["found"] for item in evidence))
        self.assertEqual(evidence[0]["body"]["withdrawal_m3"], 100.0)

    def test_chained_amendments_keep_latest_only(self) -> None:
        self.service.record_meter("c1", "P1", "2026-11-01T00:00+08:00", 100.0)
        self.service.amend_meter("c2", "P1", "2026-11-01T00:00+08:00", 110.0, "c1", "第一次更正")
        self.service.amend_meter("c3", "P1", "2026-11-01T00:00+08:00", 115.0, "c2", "第二次更正")
        result = self.recompute("P1", hours=1)
        self.assertEqual(result["total_withdrawal_m3"], 115.0)
        self.assertEqual(result["hours"][0]["basis"], ["c3"])

    def test_estimated_then_measured_backfill(self) -> None:
        self.service.record_meter(
            "est1", "P1", "2026-11-01T00:00+08:00", 80.0,
            source="estimated", estimate_source="调度台账推算",
        )
        before = self.recompute("P1", hours=1)
        self.assertTrue(before["hours"][0]["estimated"])
        self.assertEqual(before["total_estimated_m3"], 80.0)
        # 补报实测
        self.service.amend_meter(
            "fix1", "P1", "2026-11-01T00:00+08:00", 95.0, "est1", "流量计补传实测"
        )
        after = self.recompute("P1", hours=1)
        self.assertEqual(after["total_withdrawal_m3"], 95.0)
        self.assertEqual(after["total_estimated_m3"], 0.0)

    def test_backfill_must_be_measured(self) -> None:
        self.service.record_meter(
            "est2", "P1", "2026-11-01T00:00+08:00", 80.0,
            source="estimated", estimate_source="调度台账推算",
        )
        with self.assertRaisesRegex(ValueError, "实测"):
            self.service.amend_meter(
                "fix2", "P1", "2026-11-01T00:00+08:00", 90.0,
                "est2", "再次暂估", source="estimated",
            )


class MonthlyReportTests(DomainTestCase):
    def test_signed_report_is_frozen(self) -> None:
        for h in range(48):
            self.service.record_meter(
                f"nov-{h}", "P1", hour_text(h), 40.0
            )
        signed = self.service.sign_monthly_report("P1", 2026, 11, "监督员-王")
        frozen_total = signed["body"]["summary"]["total_withdrawal_m3"]
        self.assertEqual(frozen_total, 48 * 40.0)

        # 签署后补报计量更正：重新签署被拒
        self.service.amend_meter(
            "am1", "P1", "2026-11-01T00:00+08:00", 70.0, "nov-0", "补报"
        )
        from water_compliance import ReportAlreadySigned

        with self.assertRaises(ReportAlreadySigned):
            self.service.sign_monthly_report("P1", 2026, 11, "监督员-王")

        # 冻结正文不变
        body = self.service.signed_report_body("P1", 2026, 11)
        self.assertEqual(
            body["summary"]["total_withdrawal_m3"], frozen_total
        )

        # 差异调整体现 +30
        diff = self.service.monthly_difference("P1", 2026, 11)
        self.assertEqual(diff["current_summary"]["total_withdrawal_m3"], frozen_total + 30.0)
        changed = diff["changed_hours"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["delta_withdrawal_m3"], 30.0)

    def test_signed_report_unchanged_by_suspension_and_transfer(self) -> None:
        for h in range(24):
            self.service.record_meter(
                f"nov2-{h}", "P1", f"2026-11-01T{h:02d}:00+08:00", 40.0
            )
        signed = self.service.sign_monthly_report("P1", 2026, 11, "监督员-王")
        fingerprint = signed["fingerprint"]

        # 签署后追加暂停与跨主体调剂（含覆盖已履约时段）
        self.service.suspend_permit(
            "P1", "V1", "2026-11-01T10:00+08:00", "事后追责暂停",
            suspend_to="2026-11-01T12:00+08:00",
        )
        self.service.approve_transfer(
            "T9", "P1", "P2",
            "2026-11-01T00:00+08:00", "2026-11-01T04:00+08:00",
            80.0, "事后调剂批文",
        )
        body = self.service.signed_report_body("P1", 2026, 11)
        self.assertEqual(
            body["summary"]["total_withdrawal_m3"],
            signed["body"]["summary"]["total_withdrawal_m3"],
        )
        # 差异调整中可看到配额侧变化，取水侧不变
        diff = self.service.monthly_difference("P1", 2026, 11)
        quota_delta = sum(item["delta_quota_m3"] for item in diff["changed_hours"])
        self.assertNotEqual(quota_delta, 0.0)
        view = self.service.signed_report_body("P1", 2026, 11)
        self.assertEqual(view["summary"], body["summary"])
        # 指纹仍是签署时那个
        signed_record = self.service.projection.reports[("P1", "2026-11")]
        self.assertEqual(signed_record.body_fingerprint, fingerprint)
