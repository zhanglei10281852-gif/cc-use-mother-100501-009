"""应用服务层：所有写入的唯一入口，负责业务校验、幂等与审计留存。

- 命令一律通过事件账本追加，幂等键绑定业务记录标识，重复导入不会多扣额度；
- 临时增量申请在落账*之前*先做断面影响评估，评估结果按内容留存并赋予编号；
- 告警区间由核算引擎现场推导（即将违约 / 已经透支 / 恢复合规），不落地权威状态；
- evidence 接口按 basis 中的记录标识回溯到具体事件序号与原文。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import events as ev
from .engine import (
    BREACH,
    COMPLIANT,
    NEAR_BREACH,
    OVERDRAFT,
    assess_request_impact,
    recompute_point,
    section_rows,
)
from .ledger import EventStore, StoredEvent, event_key
from .projection import Projection
from .reporting import (
    DocumentStore,
    ReportAlreadySigned,
    build_monthly_body,
    difference_adjustment,
)
from .timeutil import HourInterval, iso, now

USER_KINDS = {"industrial_park", "irrigation_district", "urban_supply", "other"}
REVIEW_CONCLUSIONS = {"confirmed_violation", "waived", "pending"}
APPEAL_DECISIONS = {"upheld", "overturned"}
REQUEST_DECISIONS = {"approved", "rejected"}


class ServiceError(ValueError):
    """业务规则拒绝。"""


class ComplianceService:
    def __init__(
        self,
        store: EventStore,
        documents: DocumentStore | None = None,
    ) -> None:
        self.store = store
        self.projection = Projection()
        self.documents = documents or DocumentStore()
        for stored in store.events():
            self.projection.apply(stored)
        store.subscribe(self.projection.apply)

    # ======================================================================
    # 基础档案
    # ======================================================================

    def register_user(self, user_id: str, name: str, kind: str) -> StoredEvent:
        if kind not in USER_KINDS:
            raise ServiceError(f"主体类型必须是 {sorted(USER_KINDS)}")
        return self._append(
            ev.RegisteredWaterUser(user_id=user_id, name=name, kind=kind),
            event_key("user", user_id),
        )

    def register_point(
        self,
        point_id: str,
        name: str,
        user_id: str,
        river_reach: str,
        control_section_id: str,
        routing_lag_hours: int = 0,
        routing_factor: float = 1.0,
    ) -> StoredEvent:
        if user_id not in self.projection.users:
            raise ServiceError(f"用水主体不存在: {user_id}")
        if control_section_id not in self.projection.sections:
            raise ServiceError(f"控制断面不存在: {control_section_id}")
        if routing_lag_hours < 0 or not 0.0 <= routing_factor <= 5.0:
            raise ServiceError("汇流参数非法（滞后需≥0，系数需在 0~5）")
        return self._append(
            ev.RegisteredWithdrawalPoint(
                point_id=point_id,
                name=name,
                user_id=user_id,
                river_reach=river_reach,
                control_section_id=control_section_id,
                routing_lag_hours=routing_lag_hours,
                routing_factor=routing_factor,
            ),
            event_key("point", point_id),
        )

    def register_section(
        self, section_id: str, name: str, environmental_rules: list[tuple[str, str, str, float]]
    ) -> StoredEvent:
        self._validate_rules(environmental_rules)
        return self._append(
            ev.ControlSectionRegistered(
                section_id=section_id,
                name=name,
                environmental_rules=tuple(tuple(rule) for rule in environmental_rules),
            ),
            event_key("section", section_id),
        )

    # ======================================================================
    # 许可版本、季节额度、暂停
    # ======================================================================

    def add_permit_version(
        self,
        point_id: str,
        revision: str,
        valid_from: str,
        annual_quota_m3: float,
        purpose_codes: list[str],
        priority_subjects: list[str],
        seasonal_quotas: list[tuple[str, str, str, float]] | None = None,
        valid_to: str | None = None,
        dry_season_months: list[int] | None = None,
        note: str = "",
    ) -> StoredEvent:
        self._require_point(point_id)
        start = self._hour(valid_from)
        end = self._hour(valid_to) if valid_to else None
        if end is not None and end <= start:
            raise ServiceError("许可生效结束时间必须晚于开始时间")
        if annual_quota_m3 < 0:
            raise ServiceError("年度额度不能为负")
        if not purpose_codes:
            raise ServiceError("至少声明一项允许用途")
        seasonal = tuple(tuple(rule) for rule in (seasonal_quotas or ()))
        self._validate_rules(seasonal)
        for month in dry_season_months or []:
            if not 1 <= month <= 12:
                raise ServiceError("枯水期月份必须在 1~12")
        return self._append(
            ev.PermitVersionAdded(
                point_id=point_id,
                revision=revision,
                valid_from=iso(start),
                valid_to=iso(end) if end else None,
                annual_quota_m3=float(annual_quota_m3),
                purpose_codes=tuple(purpose_codes),
                priority_subjects=tuple(priority_subjects),
                seasonal_quotas=seasonal,
                dry_season_months=tuple(dry_season_months or ()),
                note=note,
            ),
            event_key(f"permit:{point_id}", revision),
        )

    def suspend_permit(
        self,
        point_id: str,
        revision: str,
        suspend_from: str,
        reason: str,
        suspend_to: str | None = None,
        enforcement_ref: str = "",
    ) -> StoredEvent:
        self._require_revision(point_id, revision)
        start = self._hour(suspend_from)
        end = self._hour(suspend_to) if suspend_to else None
        if end is not None and end <= start:
            raise ServiceError("暂停结束时间必须晚于开始时间")
        return self._append(
            ev.PermitSuspensionLogged(
                point_id=point_id,
                revision=revision,
                suspend_from=iso(start),
                suspend_to=iso(end) if end else None,
                reason=reason,
                enforcement_ref=enforcement_ref,
            ),
            event_key(f"susp:{point_id}", iso(start)),
        )

    def resume_permit(self, point_id: str, suspend_from: str, resume_at: str, reason: str) -> StoredEvent:
        exists = any(
            item.point_id == point_id and item.suspend_from == suspend_from
            for item in self.projection.suspensions.get(point_id, [])
        )
        if not exists:
            raise ServiceError("被恢复的暂停事件不存在")
        return self._append(
            ev.PermitResumptionLogged(
                point_id=point_id,
                suspend_from=suspend_from,
                resume_at=iso(self._hour(resume_at)),
                reason=reason,
            ),
            event_key(f"resume:{point_id}", f"{suspend_from}|{resume_at}"),
        )

    # ======================================================================
    # 逐时计量 / 退水 / 暂估 / 更正
    # ======================================================================

    def record_meter(
        self,
        record_id: str,
        point_id: str,
        hour: str,
        withdrawal_m3: float,
        source: str = "measured",
        estimate_source: str = "",
        note: str = "",
    ) -> StoredEvent:
        self._require_point(point_id)
        self._validate_reading(withdrawal_m3, source, estimate_source)
        return self._append(
            ev.HourlyMeterRecorded(
                record_id=record_id,
                point_id=point_id,
                hour=iso(self._hour(hour)),
                withdrawal_m3=float(withdrawal_m3),
                source=source,
                estimate_source=estimate_source,
                note=note,
            ),
            event_key("meter", record_id),
        )

    def record_return(
        self,
        record_id: str,
        point_id: str,
        hour: str,
        return_m3: float,
        to_section_id: str,
        source: str = "measured",
        estimate_source: str = "",
    ) -> StoredEvent:
        self._require_point(point_id)
        if to_section_id not in self.projection.sections:
            raise ServiceError(f"控制断面不存在: {to_section_id}")
        self._validate_reading(return_m3, source, estimate_source)
        return self._append(
            ev.HourlyReturnFlowRecorded(
                record_id=record_id,
                point_id=point_id,
                hour=iso(self._hour(hour)),
                return_m3=float(return_m3),
                to_section_id=to_section_id,
                source=source,
                estimate_source=estimate_source,
            ),
            event_key("return", record_id),
        )

    def amend_meter(
        self,
        amendment_id: str,
        point_id: str,
        hour: str,
        corrected_withdrawal_m3: float,
        replaces_record: str,
        reason: str,
        source: str = "measured",
    ) -> StoredEvent:
        """计量更正（含暂估补报）：差异追加，绝不删除或改写原始记录。"""
        self._require_point(point_id)
        if corrected_withdrawal_m3 < 0:
            raise ServiceError("更正水量不能为负")
        target_hour = self._resolve_target_hour(
            point_id,
            hour,
            replaces_record,
            self.projection.meter_records,
            self.projection.meter_amendments,
        )
        original = self.projection.meter_records.get(replaces_record)
        if (
            original is not None
            and original.source == "estimated"
            and source != "measured"
        ):
            raise ServiceError("暂估补报必须以实测口径提交")
        if original is None and source == "estimated":
            raise ServiceError("补报/更正必须以实测口径提交")
        return self._append(
            ev.MeterAmendmentRecorded(
                amendment_id=amendment_id,
                point_id=point_id,
                hour=target_hour,
                corrected_withdrawal_m3=float(corrected_withdrawal_m3),
                replaces_record=replaces_record,
                reason=reason,
                source=source,
            ),
            event_key("amend", amendment_id),
        )

    def amend_return(
        self,
        amendment_id: str,
        point_id: str,
        hour: str,
        corrected_return_m3: float,
        replaces_record: str,
        reason: str,
        source: str = "measured",
    ) -> StoredEvent:
        self._require_point(point_id)
        if corrected_return_m3 < 0:
            raise ServiceError("更正退水不能为负")
        target_hour = self._resolve_target_hour(
            point_id,
            hour,
            replaces_record,
            self.projection.return_records,
            self.projection.return_amendments,
        )
        return self._append(
            ev.ReturnFlowAmendmentRecorded(
                amendment_id=amendment_id,
                point_id=point_id,
                hour=target_hour,
                corrected_return_m3=float(corrected_return_m3),
                replaces_record=replaces_record,
                reason=reason,
                source=source,
            ),
            event_key("amend-return", amendment_id),
        )

    def record_section_inflow(
        self,
        record_id: str,
        section_id: str,
        hour: str,
        inflow_m3: float,
        source: str = "measured",
        estimate_source: str = "",
    ) -> StoredEvent:
        if section_id not in self.projection.sections:
            raise ServiceError(f"控制断面不存在: {section_id}")
        self._validate_reading(inflow_m3, source, estimate_source)
        return self._append(
            ev.SectionInflowRecorded(
                record_id=record_id,
                section_id=section_id,
                hour=iso(self._hour(hour)),
                inflow_m3=float(inflow_m3),
                source=source,
                estimate_source=estimate_source,
            ),
            event_key("inflow", record_id),
        )

    def amend_section_inflow(
        self,
        amendment_id: str,
        section_id: str,
        hour: str,
        corrected_inflow_m3: float,
        replaces_record: str,
        reason: str,
        source: str = "measured",
    ) -> StoredEvent:
        original = next(
            (
                item
                for item in self.projection.section_inflows.get(section_id, {}).values()
                if item.record_id == replaces_record
            ),
            None,
        )
        prior_amendment = self.projection.section_inflow_amendments.get(replaces_record)
        if original is None and prior_amendment is None:
            raise ServiceError(f"被更正的断面来水记录不存在: {replaces_record}")
        target_hour = (
            original.hour if original is not None else prior_amendment.hour
        )
        if target_hour != iso(self._hour(hour)):
            raise ServiceError("更正必须与原始记录的小时一致")
        return self._append(
            ev.SectionInflowAmendmentRecorded(
                amendment_id=amendment_id,
                section_id=section_id,
                hour=target_hour,
                corrected_inflow_m3=float(corrected_inflow_m3),
                replaces_record=replaces_record,
                reason=reason,
                source=source,
            ),
            event_key("amend-inflow", amendment_id),
        )

    # ======================================================================
    # 跨主体调剂
    # ======================================================================

    def approve_transfer(
        self,
        transfer_id: str,
        from_point_id: str,
        to_point_id: str,
        valid_from: str,
        valid_to: str,
        volume_m3: float,
        approval_ref: str,
        purpose_code: str = "transfer",
    ) -> StoredEvent:
        self._require_point(from_point_id)
        self._require_point(to_point_id)
        if from_point_id == to_point_id:
            raise ServiceError("调剂双方不能为同一取水点")
        interval = HourInterval(self._hour(valid_from), self._hour(valid_to))
        if volume_m3 <= 0:
            raise ServiceError("调剂水量必须大于零")
        if not approval_ref.strip():
            raise ServiceError("必须提供批准文号 approval_ref")
        return self._append(
            ev.TransferApproved(
                transfer_id=transfer_id,
                from_point_id=from_point_id,
                to_point_id=to_point_id,
                valid_from=interval.start_text(),
                valid_to=interval.end_text(),
                volume_m3=float(volume_m3),
                approval_ref=approval_ref,
                purpose_code=purpose_code,
            ),
            event_key("transfer", transfer_id),
        )

    def revoke_transfer(self, transfer_id: str, revoked_at: str, reason: str) -> StoredEvent:
        if transfer_id not in self.projection.transfers_raw:
            raise ServiceError(f"调剂不存在: {transfer_id}")
        return self._append(
            ev.TransferRevocationLogged(
                transfer_id=transfer_id,
                revoked_at=iso(self._hour(revoked_at)),
                reason=reason,
            ),
            event_key("transfer-revoke", transfer_id),
        )

    # ======================================================================
    # 临时增量申请：先评估、后落账
    # ======================================================================

    def assess_impact(
        self, point_id: str, valid_from: str, valid_to: str, volume_m3: float
    ) -> dict[str, Any]:
        self._require_point(point_id)
        interval = HourInterval(self._hour(valid_from), self._hour(valid_to))
        if volume_m3 <= 0:
            raise ServiceError("申请水量必须大于零")
        assessment = assess_request_impact(
            self.projection, point_id, interval, float(volume_m3), iso(now())
        )
        body = assessment.as_dict()
        ref = "impact-" + self.documents.put({"kind": "impact_assessment", **body})[:16]
        return {"assessment_ref": ref, **body}

    def submit_request(
        self,
        request_id: str,
        point_id: str,
        valid_from: str,
        valid_to: str,
        volume_m3: float,
        purpose_code: str,
        applicant: str,
        impact_assessment_ref: str,
    ) -> StoredEvent:
        self._require_point(point_id)
        interval = HourInterval(self._hour(valid_from), self._hour(valid_to))
        if volume_m3 <= 0:
            raise ServiceError("申请水量必须大于零")
        permit = self.projection.permit_at(point_id, interval.start)
        if permit is not None and purpose_code not in permit.purpose_codes:
            raise ServiceError(
                f"用途 {purpose_code!r} 不在许可用途 {list(permit.purpose_codes)} 内"
            )
        if not impact_assessment_ref.startswith("impact-"):
            raise ServiceError("必须附带提交前生成的断面影响评估编号")
        assessment_doc = self.documents.get_by_prefix(impact_assessment_ref)
        if assessment_doc is None:
            raise ServiceError("影响评估编号不存在或已失效，请先重新生成断面影响评估")
        if (
            assessment_doc.get("point_id") != point_id
            or assessment_doc.get("start") != interval.start_text()
            or assessment_doc.get("end") != interval.end_text()
            or abs(assessment_doc.get("hourly_extra_m3", 0.0) - volume_m3 / interval.hours) > 1e-6
        ):
            raise ServiceError("申请内容与所附断面影响评估不一致，请重新评估后提交")
        return self._append(
            ev.TemporaryRequestSubmitted(
                request_id=request_id,
                point_id=point_id,
                valid_from=interval.start_text(),
                valid_to=interval.end_text(),
                volume_m3=float(volume_m3),
                purpose_code=purpose_code,
                applicant=applicant,
                impact_assessment_ref=impact_assessment_ref,
            ),
            event_key("request", request_id),
        )

    def decide_request(
        self,
        request_id: str,
        decision: str,
        decided_by: str,
        reason: str,
        override: bool = False,
    ) -> StoredEvent:
        raw = self.projection.requests.get(request_id)
        if raw is None:
            raise ServiceError(f"申请不存在: {request_id}")
        if request_id in self.projection.request_decisions:
            raise ServiceError("申请已经作出决定，不得更改（如需调整请提交新申请）")
        if decision not in REQUEST_DECISIONS:
            raise ServiceError(f"决定必须是 {sorted(REQUEST_DECISIONS)}")
        extra = raw.volume_m3 if decision == "approved" else 0.0
        if decision == "approved" and not override:
            assessment = assess_request_impact(
                self.projection,
                raw.point_id,
                HourInterval(
                    datetime.fromisoformat(raw.valid_from),
                    datetime.fromisoformat(raw.valid_to),
                ),
                raw.volume_m3,
                iso(now()),
            )
            if not assessment.feasible:
                raise ServiceError(
                    "批准将新增 "
                    f"{assessment.new_breach_hours} 个生态基流破坏小时；"
                    "如坚持批准请显式 override 并留存理由"
                )
        return self._append(
            ev.TemporaryRequestDecided(
                request_id=request_id,
                decision=decision,
                decided_by=decided_by,
                reason=reason,
                approved_extra_quota_m3=float(extra),
            ),
            event_key("request-decision", request_id),
        )

    # ======================================================================
    # 月报签署（冻结）与差异调整
    # ======================================================================

    def sign_monthly_report(
        self, point_id: str, year: int, month: int, signed_by: str
    ) -> dict[str, Any]:
        self._require_point(point_id)
        period = f"{year:04d}-{month:02d}"
        if (point_id, period) in self.projection.reports:
            raise ReportAlreadySigned(f"{point_id} {period} 月报已签署，禁止重签或改写")
        body = build_monthly_body(self.projection, point_id, year, month)
        fingerprint = self.documents.put(body)
        report_id = f"report-{point_id}-{period}"
        self._append(
            ev.MonthlyReportSigned(
                report_id=report_id,
                point_id=point_id,
                period=period,
                body_fingerprint=fingerprint,
                signed_by=signed_by,
                signed_at=iso(now()),
            ),
            event_key("report-sign", f"{point_id}:{period}"),
        )
        return {"report_id": report_id, "fingerprint": fingerprint, "body": body}

    def monthly_difference(self, point_id: str, year: int, month: int) -> dict[str, Any]:
        return difference_adjustment(
            self.projection, self.documents, point_id, year, month
        )

    def signed_report_body(self, point_id: str, year: int, month: int) -> dict[str, Any]:
        period = f"{year:04d}-{month:02d}"
        signed = self.projection.reports.get((point_id, period))
        if signed is None:
            raise ServiceError(f"{point_id} {period} 尚未签署月报")
        return self.documents.get(signed.body_fingerprint)

    # ======================================================================
    # 执法复核与申诉
    # ======================================================================

    def log_review(
        self,
        review_id: str,
        point_id: str,
        valid_from: str,
        valid_to: str,
        conclusion: str,
        reviewer: str,
        note: str = "",
    ) -> StoredEvent:
        self._require_point(point_id)
        interval = HourInterval(self._hour(valid_from), self._hour(valid_to))
        if conclusion not in REVIEW_CONCLUSIONS:
            raise ServiceError(f"复核结论必须是 {sorted(REVIEW_CONCLUSIONS)}")
        return self._append(
            ev.EnforcementReviewLogged(
                review_id=review_id,
                point_id=point_id,
                valid_from=interval.start_text(),
                valid_to=interval.end_text(),
                conclusion=conclusion,
                reviewer=reviewer,
                note=note,
            ),
            event_key("review", review_id),
        )

    def log_appeal(
        self,
        appeal_id: str,
        review_id: str,
        decision: str,
        decided_by: str,
        note: str = "",
    ) -> StoredEvent:
        review = self.projection.reviews.get(review_id)
        if review is None:
            raise ServiceError(f"执法复核不存在: {review_id}")
        if decision not in APPEAL_DECISIONS:
            raise ServiceError(f"申诉决定必须是 {sorted(APPEAL_DECISIONS)}")
        return self._append(
            ev.AppealDecisionLogged(
                appeal_id=appeal_id,
                review_id=review_id,
                point_id=review.point_id,
                decision=decision,
                decided_by=decided_by,
                note=note,
            ),
            event_key("appeal", appeal_id),
        )

    # ======================================================================
    # 复算、告警与证据
    # ======================================================================

    def recompute(self, point_id: str, start: str, end: str) -> dict[str, Any]:
        interval = HourInterval(self._hour(start), self._hour(end))
        return recompute_point(self.projection, point_id, interval).as_dict()

    def section_recompute(self, section_id: str, start: str, end: str) -> dict[str, Any]:
        interval = HourInterval(self._hour(start), self._hour(end))
        rows = section_rows(self.projection, section_id, interval)
        return {
            "section_id": section_id,
            "start": interval.start_text(),
            "end": interval.end_text(),
            "breach_hours": sum(1 for row in rows if row.status == BREACH),
            "hours": [
                {
                    "hour": row.hour,
                    "inflow_m3": row.inflow_m3,
                    "estimated": row.estimated,
                    "withdrawals_routed_m3": row.withdrawals_routed_m3,
                    "returns_m3": row.returns_m3,
                    "requirement_m3": row.requirement_m3,
                    "net_m3": row.net_m3,
                    "status": row.status,
                    "basis": list(row.basis),
                }
                for row in rows
            ],
        }

    def alerts(self, point_id: str, start: str, end: str) -> dict[str, Any]:
        """自动识别三类区间：即将违约、已经透支、恢复合规。

        恢复合规 = 紧跟在非合规区间之后的合规区间；每条告警附逐时依据记录。
        """
        result = recompute_point(
            self.projection, point_id, HourInterval(self._hour(start), self._hour(end))
        )
        alerts: list[dict[str, Any]] = []
        previous_status = None
        for segment in result.status_intervals:
            kind = {
                NEAR_BREACH: "即将违约",
                OVERDRAFT: "已经透支",
                COMPLIANT: "恢复合规" if previous_status in (NEAR_BREACH, OVERDRAFT) else None,
            }.get(segment.status)
            if kind is None:
                previous_status = segment.status
                continue
            basis = sorted(
                {
                    record
                    for row in result.rows
                    if segment.start <= row.hour < segment.end
                    for record in row.basis
                }
            )
            alerts.append(
                {
                    "kind": kind,
                    "status": segment.status,
                    "start": segment.start,
                    "end": segment.end,
                    "hours": segment.hours,
                    "peak_deficit_m3": segment.peak_deficit_m3,
                    "basis_records": basis,
                }
            )
            previous_status = segment.status
        return {
            "point_id": point_id,
            "start": start,
            "end": end,
            "alerts": alerts,
            "reviews": [
                {
                    "review_id": item.review_id,
                    "conclusion": item.conclusion,
                    "appeal": item.appeal_decision,
                    "start": item.interval.start_text(),
                    "end": item.interval.end_text(),
                }
                for item in self.projection.review_views(point_id)
            ],
        }

    def evidence(self, record_ids: list[str]) -> list[dict[str, Any]]:
        """按 basis 中的记录标识回溯到事件序号、类型与原文。"""
        found: list[dict[str, Any]] = []
        for record_id in record_ids:
            seq = self.projection.event_seq_of.get(record_id)
            if seq is None:
                found.append({"record_id": record_id, "found": False})
                continue
            stored = self.store.events()[seq - 1]
            body = {name: getattr(stored.event, name) for name in getattr(stored.event, "__slots__", ())}
            found.append(
                {
                    "record_id": record_id,
                    "found": True,
                    "event_seq": seq,
                    "event_id": stored.event_id,
                    "event_type": type(stored.event).__name__,
                    "body": body,
                }
            )
        return found

    # ======================================================================
    # 内部工具
    # ======================================================================

    def _append(self, event: Any, idempotency_key: str) -> StoredEvent:
        return self.store.append(event, idempotency_key)

    def _require_point(self, point_id: str) -> None:
        if point_id not in self.projection.points:
            raise ServiceError(f"取水点不存在: {point_id}")

    def _require_revision(self, point_id: str, revision: str) -> None:
        if not any(
            permit.revision == revision
            for permit in self.projection.effective_permits(point_id)
        ):
            raise ServiceError(f"取水点 {point_id} 不存在许可版本 {revision}")

    def _resolve_target_hour(
        self,
        point_id: str,
        hour: str,
        target_record_id: str,
        records: dict[str, Any],
        amendments: dict[str, Any],
    ) -> str:
        """校验更正目标存在（原始记录或既有更正），返回规范化小时。"""
        normalized = iso(self._hour(hour))
        original = records.get(target_record_id)
        if original is not None:
            if original.point_id != point_id or original.hour != normalized:
                raise ServiceError("更正必须与原始记录的取水点和小时一致")
            return normalized
        prior = amendments.get(target_record_id)
        if prior is not None:
            if prior.point_id != point_id or prior.hour != normalized:
                raise ServiceError("再次更正必须与既有更正的取水点和小时一致")
            return normalized
        raise ServiceError(f"被更正的记录不存在: {target_record_id}")

    @staticmethod
    def _hour(text: str) -> datetime:
        return HourInterval.parse(text).start

    @staticmethod
    def _validate_reading(value: float, source: str, estimate_source: str) -> None:
        if value < 0:
            raise ServiceError("计量水量不能为负")
        if source not in ("measured", "estimated"):
            raise ServiceError("source 必须是 measured 或 estimated")
        if source == "estimated" and not estimate_source.strip():
            raise ServiceError("暂估必须注明明确来源 estimate_source（制度文件/调度令/邻站推算等）")

    @staticmethod
    def _validate_rules(rules: Any) -> None:
        for rule in rules:
            code, start_md, end_md, value = rule
            try:
                datetime.strptime(start_md, "%m-%d")
                datetime.strptime(end_md, "%m-%d")
            except ValueError as exc:
                raise ServiceError(f"季节规则 {code} 日期必须为 MM-DD") from exc
            if value < 0:
                raise ServiceError(f"季节规则 {code} 水量不能为负")
