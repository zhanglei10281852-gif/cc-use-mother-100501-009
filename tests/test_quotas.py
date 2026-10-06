"""许可版本、季节额度、用途限制、暂停与逐时核算测试。"""

from __future__ import annotations

from tests.support import DomainTestCase, hour_text


class PermitAndQuotaTests(DomainTestCase):
    def test_seasonal_quota_accrues_hourly(self) -> None:
        result = self.recompute("P1", hours=24)
        self.assertEqual(result["total_quota_m3"], 1200.0)  # 24 × 50
        self.assertEqual(result["total_withdrawal_m3"], 0.0)
        self.assertTrue(all(row["status"] == "compliant" for row in result["hours"]))

    def test_overdraft_detected_by_hour(self) -> None:
        for h in range(6):
            self.service.record_meter(
                f"m{h}", "P1", hour_text(h), 60.0
            )
        result = self.recompute("P1", hours=6)
        # 前 5 小时累计 +50*5-60*5 = -50，第 5 小时即透支
        self.assertEqual(result["hours"][4]["status"], "overdraft")
        overdraft = [s for s in result["status_intervals"] if s["status"] == "overdraft"]
        self.assertEqual(len(overdraft), 1)
        self.assertGreater(overdraft[0]["peak_deficit_m3"], 0.0)

    def test_near_breach_then_recovery_intervals(self) -> None:
        # 先按 100 m³/h 连续取水 10 小时（透支），再回到 20 m³/h 恢复
        for h in range(10):
            self.service.record_meter(
                f"hi-{h}", "P1", hour_text(h), 100.0
            )
        for h in range(10, 30):
            self.service.record_meter(
                f"lo-{h}", "P1", hour_text(h), 20.0
            )
        alerts = self.service.alerts(
            "P1", "2026-11-01T00:00+08:00", "2026-11-02T06:00+08:00"
        )
        kinds = [a["kind"] for a in alerts["alerts"]]
        self.assertIn("已经透支", kinds)
        self.assertIn("恢复合规", kinds)
        # 恢复合规区间必须出现在透支区间之后
        self.assertLess(
            next(a["start"] for a in alerts["alerts"] if a["kind"] == "已经透支"),
            next(a["start"] for a in alerts["alerts"] if a["kind"] == "恢复合规"),
        )

    def test_suspension_sets_quota_to_zero(self) -> None:
        self.service.suspend_permit(
            "P2", "V1", "2026-11-01T02:00+08:00", "应急调度",
            suspend_to="2026-11-01T05:00+08:00",
        )
        self.service.record_meter("x1", "P2", "2026-11-01T03:00+08:00", 5.0)
        result = self.service.recompute("P2", "2026-11-01T00:00+08:00", "2026-11-01T06:00+08:00")
        suspended_hours = [row for row in result["hours"] if row["suspended"]]
        self.assertEqual(len(suspended_hours), 3)
        self.assertTrue(all(row["total_quota_m3"] == 0.0 for row in suspended_hours))
        # 暂停期间取水 5，而暂停前 2 小时配额 20 → 累计余额 15，不透支
        self.assertEqual(result["hours"][-1]["status"], "compliant")

    def test_resume_is_appended_not_rewritten(self) -> None:
        self.service.suspend_permit(
            "P2", "V1", "2026-11-01T02:00+08:00", "应急调度"
        )
        self.service.resume_permit(
            "P2", "2026-11-01T02:00+08:00", "2026-11-01T04:00+08:00", "调度结束"
        )
        result = self.service.recompute("P2", "2026-11-01T00:00+08:00", "2026-11-01T06:00+08:00")
        self.assertEqual(
            [row["suspended"] for row in result["hours"]],
            [False, False, True, True, False, False],
        )

    def test_new_permit_version_truncates_previous(self) -> None:
        self.service.add_permit_version(
            point_id="P1",
            revision="V2",
            valid_from="2026-11-02T00:00+08:00",
            annual_quota_m3=1_440_000.0,
            purpose_codes=["production", "emergency"],
            priority_subjects=["工业生产"],
            seasonal_quotas=[("dry", "11-01", "11-30", 21_600.0)],  # 30 m³/h
        )
        result = self.service.recompute(
            "P1", "2026-11-01T23:00+08:00", "2026-11-02T02:00+08:00"
        )
        revisions = [(row["hour"], row["permit_revision"]) for row in result["hours"]]
        self.assertEqual(revisions[0][1], "V1")
        self.assertEqual(revisions[1][1], "V2")
        self.assertEqual(result["hours"][1]["base_quota_m3"], 30.0)

    def test_purpose_restriction_rejects_request(self) -> None:
        assessment = self.service.assess_impact(
            "P1", "2026-11-05T00:00+08:00", "2026-11-05T02:00+08:00", 10.0
        )
        with self.assertRaisesRegex(ValueError, "用途"):
            self.service.submit_request(
                "REQ1", "P1",
                "2026-11-05T00:00+08:00", "2026-11-05T02:00+08:00",
                10.0, "agriculture", "工业园", assessment["assessment_ref"],
            )


class TransferTests(DomainTestCase):
    def test_approved_transfer_lands_on_hourly_balance(self) -> None:
        self.service.approve_transfer(
            "T1", "P2", "P1",
            "2026-11-01T00:00+08:00", "2026-11-01T10:00+08:00",
            100.0, "调字01号",
        )
        p1 = self.service.recompute(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T10:00+08:00"
        )
        p2 = self.service.recompute(
            "P2", "2026-11-01T00:00+08:00", "2026-11-01T10:00+08:00"
        )
        self.assertEqual(p1["hours"][0]["transfer_in_m3"], 10.0)
        self.assertEqual(p2["hours"][0]["transfer_out_m3"], 10.0)
        self.assertEqual(p1["total_quota_m3"], 600.0)  # 500 许可 + 100 调入
        self.assertEqual(p2["total_quota_m3"], 0.0)  # 100 许可 - 100 调出

    def test_transfer_requires_approval_ref(self) -> None:
        with self.assertRaisesRegex(ValueError, "批准文号"):
            self.service.approve_transfer(
                "T2", "P2", "P1",
                "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00",
                10.0, "  ",
            )
