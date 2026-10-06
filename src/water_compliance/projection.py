"""读模型投影：从只追加事件重放得到当前全部可查询状态。

投影本身不保存任何"权威数字"——任何时候都可以从事件完整重建；
核算引擎在投影之上做纯函数式计算，因此监管人员可以对任一时段复算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from . import events as ev
from .ledger import StoredEvent
from .timeutil import BEIJING, HourInterval

INF_END = datetime(9999, 12, 31, tzinfo=BEIJING)


@dataclass(frozen=True, slots=True)
class Point:
    point_id: str
    name: str
    user_id: str
    river_reach: str
    control_section_id: str
    routing_lag_hours: int
    routing_factor: float


@dataclass(frozen=True, slots=True)
class EffectivePermit:
    point_id: str
    revision: str
    interval: HourInterval  # 生效区间（被新版本截断后的实际区间）
    raw_valid_to: str | None
    annual_quota_m3: float
    purpose_codes: tuple[str, ...]
    priority_subjects: tuple[str, ...]
    seasonal_quotas: tuple[tuple[str, str, str, float], ...]
    dry_season_months: tuple[int, ...]
    note: str


@dataclass(frozen=True, slots=True)
class MeterEntry:
    """某小时某点的一条有效计量口径。"""

    value_m3: float
    source: str  # measured | estimated
    estimate_source: str
    record_id: str  # 生效记录（可能是更正记录自身 id）
    original_record_id: str  # 最初原始记录 id
    amended: bool
    amendment_chain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Transfer:
    transfer_id: str
    from_point_id: str
    to_point_id: str
    interval: HourInterval
    hourly_m3: float
    volume_m3: float
    approval_ref: str
    purpose_code: str
    revoked: bool


@dataclass(frozen=True, slots=True)
class TemporaryRequest:
    request_id: str
    point_id: str
    interval: HourInterval
    hourly_m3: float
    volume_m3: float
    purpose_code: str
    applicant: str
    impact_assessment_ref: str
    decision: str | None
    decided_by: str
    reason: str
    approved_extra_quota_m3: float


@dataclass(frozen=True, slots=True)
class SignedReport:
    report_id: str
    point_id: str
    period: str
    body_fingerprint: str
    signed_by: str
    signed_at: str


@dataclass(frozen=True, slots=True)
class Review:
    review_id: str
    point_id: str
    interval: HourInterval
    conclusion: str
    reviewer: str
    note: str
    appeal_decision: str | None
    appeal_note: str
    appeal_by: str


class Projection:
    def __init__(self, event_stream: Iterable[StoredEvent] | None = None) -> None:
        self.users: dict[str, ev.RegisteredWaterUser] = {}
        self.points: dict[str, Point] = {}
        self.sections: dict[str, ev.ControlSectionRegistered] = {}
        self.raw_permits: dict[str, list[ev.PermitVersionAdded]] = {}
        self.suspensions: dict[str, list[ev.PermitSuspensionLogged]] = {}
        self.resumptions: list[ev.PermitResumptionLogged] = []
        self.meter_records: dict[str, ev.HourlyMeterRecorded] = {}
        self.return_records: dict[str, ev.HourlyReturnFlowRecorded] = {}
        self.meter_amendments: dict[str, ev.MeterAmendmentRecorded] = {}
        self.return_amendments: dict[str, ev.ReturnFlowAmendmentRecorded] = {}
        self.transfers_raw: dict[str, ev.TransferApproved] = {}
        self.transfer_revocations: dict[str, ev.TransferRevocationLogged] = {}
        self.requests: dict[str, ev.TemporaryRequestSubmitted] = {}
        self.request_decisions: dict[str, ev.TemporaryRequestDecided] = {}
        self.reports: dict[tuple[str, str], SignedReport] = {}
        self.reviews: dict[str, ev.EnforcementReviewLogged] = {}
        self.appeals: dict[str, ev.AppealDecisionLogged] = {}
        self.section_inflows: dict[str, dict[datetime, ev.SectionInflowRecorded]] = {}
        self.section_inflow_amendments: dict[str, ev.SectionInflowAmendmentRecorded] = {}
        self.event_seq_of: dict[str, int] = {}  # 业务记录 id -> 事件序号（取证用）
        if event_stream is not None:
            for stored in event_stream:
                self.apply(stored)

    def apply(self, stored: StoredEvent) -> None:
        event = stored.event
        t = type(event)
        if t is ev.RegisteredWaterUser:
            self.users[event.user_id] = event
        elif t is ev.RegisteredWithdrawalPoint:
            self.points[event.point_id] = Point(
                point_id=event.point_id,
                name=event.name,
                user_id=event.user_id,
                river_reach=event.river_reach,
                control_section_id=event.control_section_id,
                routing_lag_hours=event.routing_lag_hours,
                routing_factor=event.routing_factor,
            )
        elif t is ev.ControlSectionRegistered:
            self.sections[event.section_id] = event
        elif t is ev.PermitVersionAdded:
            self.raw_permits.setdefault(event.point_id, []).append(event)
            self.event_seq_of[revision_key(event.point_id, event.revision)] = stored.seq
        elif t is ev.PermitSuspensionLogged:
            self.suspensions.setdefault(event.point_id, []).append(event)
            self.event_seq_of[f"susp:{event.point_id}:{event.suspend_from}"] = stored.seq
        elif t is ev.PermitResumptionLogged:
            self.resumptions.append(event)
        elif t is ev.HourlyMeterRecorded:
            self.meter_records[event.record_id] = event
            self.event_seq_of[event.record_id] = stored.seq
        elif t is ev.HourlyReturnFlowRecorded:
            self.return_records[event.record_id] = event
            self.event_seq_of[event.record_id] = stored.seq
        elif t is ev.MeterAmendmentRecorded:
            self.meter_amendments[event.amendment_id] = event
            self.event_seq_of[event.amendment_id] = stored.seq
        elif t is ev.ReturnFlowAmendmentRecorded:
            self.return_amendments[event.amendment_id] = event
            self.event_seq_of[event.amendment_id] = stored.seq
        elif t is ev.TransferApproved:
            self.transfers_raw[event.transfer_id] = event
            self.event_seq_of[f"transfer:{event.transfer_id}"] = stored.seq
            self.event_seq_of[event.transfer_id] = stored.seq
        elif t is ev.TransferRevocationLogged:
            self.transfer_revocations[event.transfer_id] = event
        elif t is ev.TemporaryRequestSubmitted:
            self.requests[event.request_id] = event
            self.event_seq_of[f"request:{event.request_id}"] = stored.seq
            self.event_seq_of[event.request_id] = stored.seq
        elif t is ev.TemporaryRequestDecided:
            self.request_decisions[event.request_id] = event
        elif t is ev.MonthlyReportSigned:
            self.reports[(event.point_id, event.period)] = SignedReport(
                report_id=event.report_id,
                point_id=event.point_id,
                period=event.period,
                body_fingerprint=event.body_fingerprint,
                signed_by=event.signed_by,
                signed_at=event.signed_at,
            )
            self.event_seq_of[f"report:{event.report_id}"] = stored.seq
            self.event_seq_of[event.report_id] = stored.seq
        elif t is ev.EnforcementReviewLogged:
            self.reviews[event.review_id] = event
            self.event_seq_of[f"review:{event.review_id}"] = stored.seq
            self.event_seq_of[event.review_id] = stored.seq
        elif t is ev.AppealDecisionLogged:
            self.appeals[event.review_id] = event
            self.event_seq_of[f"appeal:{event.appeal_id}"] = stored.seq
            self.event_seq_of[event.appeal_id] = stored.seq
        elif t is ev.SectionInflowRecorded:
            self.section_inflows.setdefault(event.section_id, {})[
                datetime.fromisoformat(event.hour)
            ] = event
            self.event_seq_of[event.record_id] = stored.seq
        elif t is ev.SectionInflowAmendmentRecorded:
            self.section_inflow_amendments[event.amendment_id] = event
            self.event_seq_of[event.amendment_id] = stored.seq
        else:  # pragma: no cover - 防御性
            raise ValueError(f"投影无法处理事件 {t.__name__}")

    # ---- 许可版本与暂停 ----

    def effective_permits(self, point_id: str) -> list[EffectivePermit]:
        raws = sorted(self.raw_permits.get(point_id, []), key=lambda e: e.valid_from)
        result: list[EffectivePermit] = []
        for index, raw in enumerate(raws):
            start = datetime.fromisoformat(raw.valid_from)
            hard_end = datetime.fromisoformat(raw.valid_to) if raw.valid_to else INF_END
            next_start = (
                datetime.fromisoformat(raws[index + 1].valid_from) if index + 1 < len(raws) else INF_END
            )
            end = min(hard_end, next_start)
            if end <= start:
                continue
            result.append(
                EffectivePermit(
                    point_id=raw.point_id,
                    revision=raw.revision,
                    interval=HourInterval(start, end),
                    raw_valid_to=raw.valid_to,
                    annual_quota_m3=raw.annual_quota_m3,
                    purpose_codes=raw.purpose_codes,
                    priority_subjects=raw.priority_subjects,
                    seasonal_quotas=raw.seasonal_quotas,
                    dry_season_months=raw.dry_season_months,
                    note=raw.note,
                )
            )
        return result

    def permit_at(self, point_id: str, moment: datetime) -> EffectivePermit | None:
        for permit in self.effective_permits(point_id):
            if permit.interval.contains(moment):
                return permit
        return None

    def suspension_intervals(self, point_id: str) -> list[HourInterval]:
        """合并暂停事件与恢复事件后的真实暂停区间。"""
        result: list[HourInterval] = []
        for item in self.suspensions.get(point_id, []):
            start = datetime.fromisoformat(item.suspend_from)
            end = (
                datetime.fromisoformat(item.suspend_to)
                if item.suspend_to is not None
                else INF_END
            )
            for resume in self.resumptions:
                if resume.point_id == point_id and resume.suspend_from == item.suspend_from:
                    end = min(end, datetime.fromisoformat(resume.resume_at))
            if end > start:
                result.append(HourInterval(start, end))
        return result

    def is_suspended(self, point_id: str, moment: datetime) -> bool:
        return any(interval.contains(moment) for interval in self.suspension_intervals(point_id))

    # ---- 有效计量（暂估 / 更正链）----

    def _replacement_map(
        self, amendments: dict[str, Any], repl_attr: str
    ) -> dict[str, str]:
        """原始/前序记录 id -> 替代它的（最新）更正记录 id，支持连续更正。

        每条更正指向它直接替代的记录；若 A 被 B 替代、B 又被 C 替代，
        则最终 A -> C，中间更正 B 不再生效。
        """
        latest: dict[str, str] = {}
        for amendment_id, amendment in sorted(amendments.items()):
            target = getattr(amendment, repl_attr)
            # 若 target 已被更早的更正替代，把新更正挂到链首
            root = target
            while root in latest:
                root = latest[root]
            latest[root] = amendment_id
            # 保证 target 本身也能解析到最新
            if target != root:
                latest[target] = amendment_id
        return latest

    @staticmethod
    def _chain_to_latest(original_id: str, replaced_by: dict[str, str]) -> tuple[str, ...]:
        chain: list[str] = []
        current = original_id
        while current in replaced_by:
            current = replaced_by[current]
            chain.append(current)
        return tuple(chain)

    def effective_meter(self, point_id: str) -> dict[datetime, list[MeterEntry]]:
        return self._effective_series(
            point_id,
            self.meter_records,
            self.meter_amendments,
            value_attr="withdrawal_m3",
            repl_attr="replaces_record",
            amendment_value="corrected_withdrawal_m3",
        )

    def effective_returns(self, point_id: str) -> dict[datetime, list[MeterEntry]]:
        return self._effective_series(
            point_id,
            self.return_records,
            self.return_amendments,
            value_attr="return_m3",
            repl_attr="replaces_record",
            amendment_value="corrected_return_m3",
        )

    def _effective_series(
        self,
        point_id: str,
        records: dict[str, Any],
        amendments: dict[str, Any],
        value_attr: str,
        repl_attr: str,
        amendment_value: str,
    ) -> dict[datetime, list[MeterEntry]]:
        replaced_by = self._replacement_map(amendments, repl_attr)
        # 找出所有被替代的原始记录
        superseded_originals = {
            original for original in replaced_by if original in records
        }
        # 找出被后续更正取代的中间更正
        superseded_amendments = {
            amendment_id
            for amendment_id in amendments
            if amendment_id in replaced_by
        }

        series: dict[datetime, list[MeterEntry]] = {}

        for record_id, record in records.items():
            if record.point_id != point_id or record_id in superseded_originals:
                continue
            hour = datetime.fromisoformat(record.hour)
            series.setdefault(hour, []).append(
                MeterEntry(
                    value_m3=getattr(record, value_attr),
                    source=record.source,
                    estimate_source=getattr(record, "estimate_source", ""),
                    record_id=record_id,
                    original_record_id=record_id,
                    amended=False,
                    amendment_chain=(),
                )
            )
        for amendment_id, amendment in amendments.items():
            if amendment.point_id != point_id or amendment_id in superseded_amendments:
                continue
            original_id = amendment.replaces_record
            hour = datetime.fromisoformat(amendment.hour)
            series.setdefault(hour, []).append(
                MeterEntry(
                    value_m3=getattr(amendment, amendment_value),
                    source=amendment.source,
                    estimate_source="",
                    record_id=amendment_id,
                    original_record_id=original_id,
                    amended=True,
                    amendment_chain=self._chain_to_latest(original_id, replaced_by),
                )
            )
        return series

    # ---- 调剂与临时申请 ----

    def transfers(self) -> list[Transfer]:
        result: list[Transfer] = []
        for transfer_id, raw in self.transfers_raw.items():
            start = datetime.fromisoformat(raw.valid_from)
            full_end = datetime.fromisoformat(raw.valid_to)
            full_hours = int((full_end - start).total_seconds() // 3600)
            hourly = raw.volume_m3 / full_hours
            revocation = self.transfer_revocations.get(transfer_id)
            revoked = revocation is not None
            end = full_end
            if revoked:
                end = min(end, datetime.fromisoformat(revocation.revoked_at))
            if end <= start:
                continue
            interval = HourInterval(start, end)
            result.append(
                Transfer(
                    transfer_id=transfer_id,
                    from_point_id=raw.from_point_id,
                    to_point_id=raw.to_point_id,
                    interval=interval,
                    hourly_m3=hourly,
                    volume_m3=round(hourly * interval.hours, 6),
                    approval_ref=raw.approval_ref,
                    purpose_code=raw.purpose_code,
                    revoked=revoked,
                )
            )
        return sorted(result, key=lambda item: (item.interval.start, item.transfer_id))

    def temporary_requests(self, point_id: str | None = None) -> list[TemporaryRequest]:
        result: list[TemporaryRequest] = []
        for request_id, raw in self.requests.items():
            if point_id is not None and raw.point_id != point_id:
                continue
            interval = HourInterval(
                datetime.fromisoformat(raw.valid_from),
                datetime.fromisoformat(raw.valid_to),
            )
            decision = self.request_decisions.get(request_id)
            result.append(
                TemporaryRequest(
                    request_id=request_id,
                    point_id=raw.point_id,
                    interval=interval,
                    hourly_m3=raw.volume_m3 / interval.hours,
                    volume_m3=raw.volume_m3,
                    purpose_code=raw.purpose_code,
                    applicant=raw.applicant,
                    impact_assessment_ref=raw.impact_assessment_ref,
                    decision=decision.decision if decision else None,
                    decided_by=decision.decided_by if decision else "",
                    reason=decision.reason if decision else "",
                    approved_extra_quota_m3=(
                        decision.approved_extra_quota_m3 if decision else 0.0
                    ),
                )
            )
        return sorted(result, key=lambda item: (item.interval.start, item.request_id))

    # ---- 断面来水有效序列（暂估 / 更正链）----

    def effective_section_inflows(
        self, section_id: str
    ) -> dict[datetime, MeterEntry]:
        records = {
            item.record_id: item
            for item in self.section_inflows.get(section_id, {}).values()
        }
        amendments = {
            aid: item
            for aid, item in self.section_inflow_amendments.items()
            if item.section_id == section_id
        }
        replaced_by = self._replacement_map(amendments, "replaces_record")
        series: dict[datetime, MeterEntry] = {}
        for record_id, record in records.items():
            if record_id in replaced_by:
                continue
            hour = datetime.fromisoformat(record.hour)
            series[hour] = MeterEntry(
                value_m3=record.inflow_m3,
                source=record.source,
                estimate_source=record.estimate_source,
                record_id=record_id,
                original_record_id=record_id,
                amended=False,
                amendment_chain=(),
            )
        for amendment_id, amendment in amendments.items():
            if amendment_id in replaced_by:
                continue
            hour = datetime.fromisoformat(amendment.hour)
            series[hour] = MeterEntry(
                value_m3=amendment.corrected_inflow_m3,
                source=amendment.source,
                estimate_source="",
                record_id=amendment_id,
                original_record_id=amendment.replaces_record,
                amended=True,
                amendment_chain=self._chain_to_latest(amendment.replaces_record, replaced_by),
            )
        return series

    # ---- 执法 / 申诉 ----
    def review_views(self, point_id: str | None = None) -> list[Review]:
        result: list[Review] = []
        for review_id, raw in self.reviews.items():
            if point_id is not None and raw.point_id != point_id:
                continue
            appeal = self.appeals.get(review_id)
            result.append(
                Review(
                    review_id=review_id,
                    point_id=raw.point_id,
                    interval=HourInterval(
                        datetime.fromisoformat(raw.valid_from),
                        datetime.fromisoformat(raw.valid_to),
                    ),
                    conclusion=raw.conclusion,
                    reviewer=raw.reviewer,
                    note=raw.note,
                    appeal_decision=appeal.decision if appeal else None,
                    appeal_note=appeal.note if appeal else "",
                    appeal_by=appeal.decided_by if appeal else "",
                )
            )
        return sorted(result, key=lambda item: (item.interval.start, item.review_id))


def revision_key(point_id: str, revision: str) -> str:
    return f"permit:{point_id}:{revision}"
