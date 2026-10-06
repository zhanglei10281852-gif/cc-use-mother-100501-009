"""生态控制断面、临时申请影响评估、执法复核与申诉测试。"""

from __future__ import annotations

from tests.support import DomainTestCase, hour_text


class SectionImpactTests(DomainTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 断面来水：600 m³/h，基流要求 500 → 断面余量 100 m³/h
        for h in range(48):
            self.service.record_section_inflow(
                f"inf-{h}", "S1", hour_text(h), 600.0
            )
        for h in range(48):
            self.service.record_meter(
                f"pm-{h}", "P1", hour_text(h), 40.0
            )

    def test_section_compliant_with_baseline(self) -> None:
        result = self.service.section_recompute(
            "S1", "2026-11-01T00:00+08:00", "2026-11-02T00:00+08:00"
        )
        # 600 - 40(P1) - 0.5*0(P2) = 560 ≥ 500
        self.assertEqual(result["breach_hours"], 0)
        self.assertTrue(all(row["net_m3"] == 560.0 for row in result["hours"]))

    def test_routing_lag_and_factor_applied(self) -> None:
        # P2 在 t 取水 200，滞后 2h、系数 0.5 → t+2 断面减少 100
        self.service.record_meter(
            "irr-hi", "P2", "2026-11-01T00:00+08:00", 200.0
        )
        result = self.service.section_recompute(
            "S1", "2026-11-01T00:00+08:00", "2026-11-01T05:00+08:00"
        )
        nets = {row["hour"][-14:]: row["withdrawals_routed_m3"] for row in result["hours"]}
        # t+2 即 02 点，P1 的 40 始终在；P2 贡献 100
        hour02 = next(row for row in result["hours"] if row["hour"].endswith("T02:00+08:00"))
        self.assertEqual(hour02["withdrawals_routed_m3"], 140.0)
        self.assertEqual(hour02["net_m3"], 460.0)
        self.assertEqual(hour02["status"], "breach")

    def test_impact_assessment_blocks_new_breach(self) -> None:
        # 申请每小时额外 100 → 断面 460 < 500，每个小时都新增破坏
        assessment = self.service.assess_impact(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T05:00+08:00", 500.0
        )
        self.assertFalse(assessment["feasible"])
        self.assertEqual(assessment["new_breach_hours"], 5)
        self.assertEqual(assessment["baseline_breach_hours"], 0)

    def test_request_requires_assessment_before_submit(self) -> None:
        with self.assertRaisesRegex(ValueError, "影响评估编号"):
            self.service.submit_request(
                "REQ-X", "P1",
                "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00",
                10.0, "production", "工业园", "not-an-assessment",
            )

    def test_decide_rejects_when_infeasible_then_override_audited(self) -> None:
        assessment = self.service.assess_impact(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00", 400.0
        )
        self.service.submit_request(
            "REQ1", "P1",
            "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00",
            400.0, "production", "工业园", assessment["assessment_ref"],
        )
        with self.assertRaisesRegex(ValueError, "生态基流"):
            self.service.decide_request("REQ1", "approved", "审批人", "先批了再说")
        decided = self.service.decide_request(
            "REQ1", "approved", "应急指挥部", "民生应急，override", override=True
        )
        self.assertEqual(decided.seq, self.service.store.version())
        # 批准后临时额度进入逐时核算
        result = self.service.recompute(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00"
        )
        self.assertEqual(result["hours"][0]["temp_quota_m3"], 200.0)

    def test_feasible_request_approved_normally(self) -> None:
        assessment = self.service.assess_impact(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00", 20.0
        )
        self.assertTrue(assessment["feasible"])

    def test_decision_is_immutable(self) -> None:
        assessment = self.service.assess_impact(
            "P1", "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00", 10.0
        )
        self.service.submit_request(
            "REQ2", "P1",
            "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00",
            10.0, "production", "工业园", assessment["assessment_ref"],
        )
        self.service.decide_request("REQ2", "rejected", "审批人", "余量不足")
        with self.assertRaisesRegex(ValueError, "不得更改"):
            self.service.decide_request("REQ2", "approved", "审批人", "翻案")


class EnforcementTests(DomainTestCase):
    def test_review_and_appeal_preserved(self) -> None:
        self.service.log_review(
            "RV1", "P1",
            "2026-11-01T00:00+08:00", "2026-11-01T04:00+08:00",
            "confirmed_violation", "王监督", "连续超许可",
        )
        self.service.log_appeal(
            "AP1", "RV1", "overturned", "复议委员会", "计量口径错误，撤销"
        )
        views = self.service.projection.review_views("P1")
        self.assertEqual(views[0].conclusion, "confirmed_violation")
        self.assertEqual(views[0].appeal_decision, "overturned")
        # 原始复核结论未被申诉改写
        evidence = self.service.evidence(["RV1", "AP1"])
        self.assertEqual(evidence[0]["body"]["conclusion"], "confirmed_violation")
        self.assertEqual(evidence[1]["body"]["decision"], "overturned")

    def test_invalid_conclusion_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "复核结论"):
            self.service.log_review(
                "RV2", "P1",
                "2026-11-01T00:00+08:00", "2026-11-01T02:00+08:00",
                "guilty", "王", "",
            )
