"""履约核算端到端规则测试。"""

from __future__ import annotations

import unittest

from water_compliance import ComplianceService, ContentConflict, Ledger
from water_compliance.service import ServiceError


def build_scenario() -> ComplianceService:
    """工业园 + 灌区两个许可、一个下游生态控制断面的枯水期场景。"""
    svc = ComplianceService(Ledger(":memory:"))
    svc.register_permit(
        permit_id="P-IND", point="PT-A", subject_id="S-IND",
        subject_name="滨河工业园", use_codes=["industrial"],
        priority_order=3, hourly_limit_m3=100.0,
        effective_from="2026-09-01T00", doc_ref="证-001")
    svc.register_permit(
        permit_id="P-IRR", point="PT-B", subject_id="S-IRR",
        subject_name="东灌区", use_codes=["irrigation", "ecological"],
        priority_order=2, hourly_limit_m3=200.0,
        effective_from="2026-09-01T00", doc_ref="证-002")
    # 城市供水：优先保障对象
    svc.register_permit(
        permit_id="P-CITY", point="PT-A", subject_id="S-CITY",
        subject_name="城市供水公司", use_codes=["domestic"],
        priority_order=1, hourly_limit_m3=300.0,
        effective_from="2026-09-01T00", doc_ref="证-003")

    svc.open_quota(
        quota_id="Q-IND-DRY", permit_id="P-IND", use_code="industrial",
        season_label="枯水期", start_hour="2026-10-01T00",
        end_hour="2026-11-01T00", quota_m3=1000.0, doc_ref="额度-001")
    svc.open_quota(
        quota_id="Q-IRR-DRY", permit_id="P-IRR", use_code="irrigation",
        season_label="枯水期", start_hour="2026-10-01T00",
        end_hour="2026-11-01T00", quota_m3=5000.0, doc_ref="额度-002")

    svc.register_section(section_id="SEC-1", name="生态控制断面甲",
                         eco_flow_m3=500.0, doc_ref="区划-001")
    svc.schedule_section_requirement(
        schedule_id="SCH-1", section_id="SEC-1",
        start_hour="2026-10-01T00", end_hour="2026-11-01T00",
        eco_flow_m3=400.0, doc_ref="枯水期生态流量")
    # PT-A 取水滞后 2 小时影响断面，耗水系数 0.8；PT-B 滞后 1 小时，系数 0.5
    svc.link_section_point(
        link_id="L-1", section_id="SEC-1", point="PT-A",
        lag_hours=2, consumptive_factor=0.8, doc_ref="水文关系-1")
    svc.link_section_point(
        link_id="L-2", section_id="SEC-1", point="PT-B",
        lag_hours=1, consumptive_factor=0.5, doc_ref="水文关系-2")
    return svc


class TimeBucketTests(unittest.TestCase):
    def test_hour_parse_and_range(self) -> None:
        from water_compliance.timebuckets import hours_between, month_of
        self.assertEqual(month_of("2026-10-05T14"), "2026-10")
        hours = hours_between("2026-10-05T14", "2026-10-05T16")
        self.assertEqual(hours, ["2026-10-05T14", "2026-10-05T15"])
        with self.assertRaises(ValueError):
            hours_between("2026-10-05T16", "2026-10-05T14")


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_raw_record_does_not_double_charge(self) -> None:
        svc = build_scenario()
        first = svc.record_measurement(
            "R-1", "P-IND", "2026-10-05T10", "industrial", 60.0,
            source="在线表", observed_month="2026-10")
        second = svc.record_measurement(
            "R-1", "P-IND", "2026-10-05T10", "industrial", 60.0,
            source="在线表", observed_month="2026-10")
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.seq, second.seq)
        avail = svc.availability("P-IND", "industrial", "2026-10-05T10")
        self.assertEqual(avail["used_this_hour_m3"], 60.0)
        self.assertEqual(avail["quotas"][0]["used_m3"], 60.0)

    def test_same_raw_id_different_content_conflicts(self) -> None:
        svc = build_scenario()
        svc.record_measurement("R-1", "P-IND", "2026-10-05T10",
                               "industrial", 60.0, source="在线表")
        # 同号不同内容：服务层拒绝（底层为 ContentConflict），不得静默覆盖
        with self.assertRaises(ServiceError):
            svc.record_measurement("R-1", "P-IND", "2026-10-05T10",
                                   "industrial", 90.0, source="在线表")
        # 原 60.0 仍然有效，额度没有被 90 覆盖
        avail = svc.availability("P-IND", "industrial", "2026-10-05T10")
        self.assertEqual(avail["used_this_hour_m3"], 60.0)


class EstimateAndAmendmentTests(unittest.TestCase):
    def test_estimate_requires_source(self) -> None:
        svc = build_scenario()
        with self.assertRaises(ServiceError):
            svc.record_measurement("R-2", "P-IND", "2026-10-05T11",
                                   "industrial", 70.0, source="", estimate=True)

    def test_backfill_produces_delta_only(self) -> None:
        svc = build_scenario()
        svc.record_measurement(
            "R-3", "P-IND", "2026-10-05T12", "industrial", 50.0,
            source="调度规程暂估", estimate=True, observed_month="2026-10")
        before = svc.availability("P-IND", "industrial", "2026-10-05T12")
        self.assertTrue(before["quotas"][0]["evidence"])
        # 实测补报为 65：只登记 +15 的差异调整
        svc.amend_measurement(
            "A-3", "R-3", 15.0, "backfill", "实测表读数补传",
            source="在线表", observed_month="2026-11")
        after = svc.availability("P-IND", "industrial", "2026-10-05T12")
        self.assertEqual(after["quotas"][0]["used_m3"], 65.0)
        self.assertFalse(after["quotas"][0]["estimate"])
        # 季节额度用量的证据链恰好是：暂估原始记录 + 补报差异
        self.assertEqual(len(after["quotas"][0]["evidence"]), 2)
        # 原事件仍在台账中且内容未变
        base_seq = after["quotas"][0]["evidence"][0]
        original = svc.ledger.get(base_seq)
        self.assertIsNotNone(original)
        self.assertEqual(original.event_type, "measurement_recorded")
        self.assertEqual(original.payload["gross_m3"], 50.0)


class PermitGovernanceTests(unittest.TestCase):
    def test_suspension_blocks_application_and_flags_withdrawal(self) -> None:
        svc = build_scenario()
        svc.suspend_permit(
            permit_id="P-IND", from_hour="2026-10-06T00",
            to_hour="2026-10-07T00", reason="超计划取水责令暂停", doc_ref="执法-1")
        assessment = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-06T08", "2026-10-06T10", 10.0)
        self.assertEqual(assessment["decision"], "rejected")
        self.assertTrue(any(v["rule"] == "permit_suspended"
                            for v in assessment["violations"]))
        # 暂停期间仍取水 → 逐时状态透支，证据指向暂停事件
        svc.record_measurement("R-4", "P-IND", "2026-10-06T08",
                               "industrial", 20.0, source="在线表")
        report = svc.compliance("2026-10-06T08", "2026-10-06T09")
        hourly = report["permits"]["P-IND"]["hourly"][0]
        self.assertIn("withdrawal_while_suspended", hourly["reasons"])
        self.assertEqual(hourly["state"], "overdraft")

    def test_use_restriction_and_revision_window(self) -> None:
        svc = build_scenario()
        # 工业园不允许 domestic 用途
        assessment = svc.evaluate_application(
            "P-IND", "domestic", "2026-10-05T00", "2026-10-05T02", 5.0)
        self.assertTrue(any(v["rule"] == "use_not_authorized"
                            for v in assessment["violations"]))
        # 10 日起修订小时限值为 40
        svc.revise_permit(
            permit_id="P-IND", revision_seq="REV-1", use_codes=["industrial"],
            priority_order=3, hourly_limit_m3=40.0,
            valid_from="2026-10-10T00", doc_ref="证-001-变1")
        over = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-10T00", "2026-10-10T02", 50.0)
        self.assertTrue(any(v["rule"] == "hourly_limit_exceeded"
                            for v in over["violations"]))
        # 修订前旧限值仍然有效
        before = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-09T23", "2026-10-10T00", 50.0)
        self.assertEqual(before["decision"], "approved")


class QuotaAndTransferTests(unittest.TestCase):
    def test_transfer_moves_budget_both_sides(self) -> None:
        svc = build_scenario()
        svc.approve_transfer(
            "T-1", from_permit="P-IRR", to_permit="P-IND",
            event_hour="2026-10-08T00", use_code="industrial",
            amount_m3=200.0, doc_ref="调剂批复-1")
        # 工业用途：灌区无 industrial 额度，不影响其灌溉额度；工业园额度增加
        # 调剂按 use_code 匹配，这里给工业园 industrial 增 200
        svc.record_measurement("R-5", "P-IND", "2026-10-08T01",
                               "industrial", 1100.0, source="在线表")
        avail = svc.availability("P-IND", "industrial", "2026-10-08T01")
        # 1000 基础 + 200 调入 - 1100 已用 = 100
        self.assertEqual(avail["quotas"][0]["remaining_m3"], 100.0)

    def test_overdraft_detection(self) -> None:
        svc = build_scenario()
        svc.record_measurement("R-6", "P-IND", "2026-10-08T01",
                               "industrial", 1050.0, source="在线表")
        alerts = svc.alert_basis("2026-10-08T00", "2026-10-08T03")
        intervals = alerts["permits"]["P-IND"]
        self.assertTrue(any(i["state"] == "overdraft" for i in intervals))
        # 告警可追溯到原始记录
        basis = intervals[0]["basis_records"]
        self.assertTrue(any(e["event_type"] == "measurement_recorded" for e in basis))

    def test_recovery_interval_is_marked(self) -> None:
        svc = build_scenario()
        svc.record_measurement("R-7", "P-IND", "2026-10-08T01",
                               "industrial", 1050.0, source="在线表")
        # 次日增加额度修订（正式追加调整）
        svc.amend_quota(amendment_id="QA-1", quota_id="Q-IND-DRY",
                        delta_m3=200.0, effective_hour="2026-10-09T00",
                        reason="区域统筹追加", doc_ref="额度调整-1")
        alerts = svc.alert_basis("2026-10-08T00", "2026-10-09T03")
        intervals = alerts["permits"]["P-IND"]
        # 10-09T00 起额度追加后恢复合规
        recovered = [i for i in intervals if i.get("recovered")]
        self.assertTrue(recovered)
        self.assertEqual(recovered[0]["prior_state"], "overdraft")


class PriorityTests(unittest.TestCase):
    def test_priority_subject_may_overdraw_quota_but_not_eco_flow(self) -> None:
        svc = build_scenario()
        svc.open_quota(
            quota_id="Q-CITY-DRY", permit_id="P-CITY", use_code="domestic",
            season_label="枯水期", start_hour="2026-10-01T00",
            end_hour="2026-11-01T00", quota_m3=100.0, doc_ref="额度-003")
        svc.record_measurement("C-1", "P-CITY", "2026-10-05T09",
                               "domestic", 100.0, source="在线表")
        # 城市供水为优先保障对象：额度已尽，申请仍可批准，但留下优先占用警告
        assessment = svc.evaluate_application(
            "P-CITY", "domestic", "2026-10-05T20", "2026-10-05T21", 50.0)
        self.assertEqual(assessment["decision"], "approved")
        self.assertTrue(any(w["rule"] == "seasonal_quota_priority_overdraw"
                            for w in assessment["warnings"]))
        # 普通工业园同样额度不足则被否决
        svc.record_measurement("C-2", "P-IND", "2026-10-05T09",
                               "industrial", 1000.0, source="在线表")
        normal = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-05T20", "2026-10-05T21", 50.0)
        self.assertEqual(normal["decision"], "rejected")
        self.assertTrue(any(v["rule"] == "seasonal_quota_exceeded"
                            for v in normal["violations"]))
        # 优先对象同样不能突破生态硬约束
        svc.record_inflow("C-3", "SEC-1", "2026-10-05T22", 600.0,
                          source="水文站")
        blocked = svc.evaluate_application(
            "P-CITY", "domestic", "2026-10-05T20", "2026-10-05T21", 800.0)
        self.assertEqual(blocked["decision"], "rejected")
        self.assertTrue(any(v["rule"] in {"eco_flow_breach", "hourly_limit_exceeded"}
                            for v in blocked["violations"]))


class SectionImpactTests(unittest.TestCase):
    def test_application_checks_downstream_eco_flow_with_lag(self) -> None:
        svc = build_scenario()
        # 入库 600，灌溉退水 100；净耗水约 100*0.5=50（无退水时按系数）
        svc.record_inflow("F-1", "SEC-1", "2026-10-05T12", 600.0,
                          source="水文站", observed_month="2026-10")
        svc.record_measurement("W-1", "P-IRR", "2026-10-05T11",
                               "irrigation", 100.0, source="在线表")
        svc.record_return("D-1", "P-IRR", "2026-10-05T11", 20.0,
                          source="退水口监测")
        # 工业园在 10 点申请取水 300，2 小时后（12 点）影响断面：
        # 600 - (100-20) - 300*0.8 = 340 < 400 生态要求 → 拒绝
        assessment = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-05T10", "2026-10-05T11", 300.0)
        self.assertEqual(assessment["decision"], "rejected")
        breaches = [v for v in assessment["violations"]
                    if v["rule"] == "eco_flow_breach"]
        self.assertTrue(breaches)
        self.assertEqual(breaches[0]["hour"], "2026-10-05T12")

    def test_missing_inflow_is_flagged_not_silently_compliant(self) -> None:
        svc = build_scenario()
        assessment = svc.evaluate_application(
            "P-IND", "industrial", "2026-10-05T10", "2026-10-05T11", 10.0)
        check = assessment["section_checks"][0]
        self.assertEqual(check["state"], "inflow_data_missing")

    def test_section_breach_and_recovery(self) -> None:
        svc = build_scenario()
        svc.record_inflow("F-2", "SEC-1", "2026-10-05T10", 350.0,
                          source="水文站")
        report = svc.compliance("2026-10-05T10", "2026-10-05T12")
        self.assertEqual(report["sections"]["SEC-1"]["hourly"][0]["state"],
                         "breach")
        svc.record_inflow("F-3", "SEC-1", "2026-10-05T11", 900.0,
                          source="水文站")
        alerts = svc.alert_basis("2026-10-05T10", "2026-10-05T12")
        intervals = alerts["sections"]["SEC-1"]
        states = [(i["state"], i.get("recovered")) for i in intervals]
        self.assertIn(("breach", False), states)
        self.assertTrue(any(rec for _, rec in states))


class MonthlyReportTests(unittest.TestCase):
    def test_signed_report_is_immutable_and_adjustments_are_listed(self) -> None:
        svc = build_scenario()
        svc.record_measurement(
            "M-1", "P-IND", "2026-10-05T10", "industrial", 50.0,
            source="调度规程暂估", estimate=True, observed_month="2026-10")
        signed = svc.sign_report("2026-10", "监督员甲", "月报-202610")
        self.assertFalse(signed.duplicate)
        # 同月不可重复签署
        with self.assertRaises(ServiceError):
            svc.sign_report("2026-10", "监督员甲", "月报-20202610")
        # 11 月补报 10 月数据：+8 差异，不得改写已签署报告
        svc.amend_measurement(
            "AM-1", "M-1", 8.0, "backfill", "实测数据补传",
            source="在线表", observed_month="2026-11")
        svc.record_measurement(
            "M-2", "P-IND", "2026-10-06T10", "industrial", 30.0,
            source="在线表", observed_month="2026-11")
        verification = svc.verify_report("2026-10")
        self.assertTrue(verification["intact"])
        adjustment_months = [
            e["payload"]["observed_month"]
            for e in verification["post_signing_adjustments"]]
        self.assertIn("2026-11", adjustment_months)
        # 签署后的许可暂停不得重写历史月报口径
        svc.suspend_permit(
            permit_id="P-IND", from_hour="2026-10-03T00",
            to_hour="2026-10-04T00", reason="事后检查", doc_ref="执法-2")
        self.assertTrue(svc.verify_report("2026-10")["intact"])

    def test_report_contains_permit_and_section_figures(self) -> None:
        svc = build_scenario()
        svc.record_measurement("M-3", "P-IND", "2026-10-05T10",
                               "industrial", 120.0, source="在线表")
        svc.record_inflow("F-4", "SEC-1", "2026-10-05T10", 300.0,
                          source="水文站")
        snapshot = svc.build_monthly_report("2026-10")
        self.assertEqual(snapshot["permits"]["P-IND"]["withdrawn_m3"], 120.0)
        self.assertTrue(snapshot["sections"]["SEC-1"]["breach_hours"])
        self.assertTrue(snapshot["basis_events"])


class EnforcementTests(unittest.TestCase):
    def test_appeal_conclusion_is_retained(self) -> None:
        svc = build_scenario()
        svc.record_enforcement(
            case_id="CASE-1", scope_type="permit", target="P-IND",
            start_hour="2026-10-05T10", end_hour="2026-10-05T12",
            status="confirmed", conclusion="认定超季节额度取水",
            reviewer="执法员乙", observed_month="2026-10")
        svc.record_enforcement(
            case_id="CASE-1-R", scope_type="permit", target="P-IND",
            start_hour="2026-10-05T10", end_hour="2026-10-05T12",
            status="appeal_upheld", conclusion="申诉成立：计量暂估口径有误",
            reviewer="复议委员会", observed_month="2026-11",
            related_case="CASE-1")
        proj = svc.projection()
        statuses = [e["status"] for e in proj.enforcement]
        self.assertEqual(statuses, ["confirmed", "appeal_upheld"])
        # 原始认定未被删除
        self.assertEqual(proj.enforcement[0]["conclusion"], "认定超季节额度取水")


class ReferentialRuleTests(unittest.TestCase):
    def test_unknown_references_rejected(self) -> None:
        svc = build_scenario()
        with self.assertRaises(ServiceError):
            svc.record_measurement("X-1", "P-GHOST", "2026-10-05T10",
                                   "industrial", 10.0, source="在线表")
        with self.assertRaises(ServiceError):
            svc.amend_measurement("X-2", "NO-SUCH-RAW", 1.0, "correction",
                                  "x", "在线表", "2026-10")
        with self.assertRaises(ServiceError):
            svc.approve_transfer("X-3", "P-IND", "P-GHOST",
                                 "2026-10-05T10", "industrial", 5.0, "批复-x")
        with self.assertRaises(ServiceError):
            svc.decide_application("NO-SUCH-APP", "approved", "x", "2026-10")

    def test_decided_application_cannot_be_redecided(self) -> None:
        svc = build_scenario()
        svc.submit_application(
            "A-1", "P-IND", "industrial",
            "2026-10-05T20", "2026-10-05T21", 10.0)
        with self.assertRaises(ServiceError):
            svc.decide_application("A-1", "rejected", "人工改判", "2026-10")

    def test_enforcement_requires_valid_target_and_case(self) -> None:
        svc = build_scenario()
        with self.assertRaises(ServiceError):
            svc.record_enforcement(
                case_id="C-X", scope_type="permit", target="P-GHOST",
                status="open", conclusion="x", reviewer="r",
                observed_month="2026-10")
        with self.assertRaises(ServiceError):
            svc.record_enforcement(
                case_id="C-X2", scope_type="permit", target="P-IND",
                status="appeal_upheld", conclusion="x", reviewer="r",
                observed_month="2026-10", related_case="MISSING")


class RecomputationTests(unittest.TestCase):
    def test_rebuild_from_events_matches(self) -> None:
        svc = build_scenario()
        svc.record_measurement("RC-1", "P-IND", "2026-10-05T10",
                               "industrial", 80.0, source="在线表")
        first = svc.compliance("2026-10-05T10", "2026-10-05T11")
        rebuilt = ComplianceService(Ledger(":memory:"))
        for event in svc.ledger.all_events():
            rebuilt._post(event.event_type, event.payload)  # noqa: SLF001
        second = rebuilt.compliance("2026-10-05T10", "2026-10-05T11")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
