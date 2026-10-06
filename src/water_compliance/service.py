"""应用服务：API 与命令行共用的用例层。

所有规则集中在这里：

* 暂估必须注明来源；补报只能以更正事件表达差异，绝不覆盖原始记录。
* 已签署月份之后入账的更正属于以后月份的“差异调整”，旧月报仍可按原口径验签。
* 新增临时申请前，必须逐时评估许可小时限值、季节额度余量与下游断面生态流量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from .events import Event
from .projection import Projection
from .storage import ContentConflict, Ledger
from .timebuckets import add_hours, hours_between, month_of, parse_hour


class ServiceError(Exception):
    """业务规则拒绝。"""


def canon_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class PostResult:
    seq: int
    duplicate: bool
    event_type: str


class ComplianceService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    # --------------------------------------------------------------- 内部工具
    def _post(self, event_type: str, payload: dict[str, Any]) -> PostResult:
        try:
            event, duplicate = self.ledger.append(event_type, payload)
        except ContentConflict as exc:
            raise ServiceError(str(exc)) from exc
        return PostResult(seq=event.seq or 0, duplicate=duplicate,
                          event_type=event_type)

    def projection(self) -> Projection:
        return Projection(self.ledger.all_events())

    def _require_permit(self, permit_id: str) -> Projection:
        proj = self.projection()
        if permit_id not in proj.permits:
            raise ServiceError(f"许可不存在: {permit_id}")
        return proj

    def _require_section(self, section_id: str) -> Projection:
        proj = self.projection()
        if section_id not in proj.sections:
            raise ServiceError(f"控制断面不存在: {section_id}")
        return proj

    def _require_raw(self, proj: Projection, raw_id: str) -> None:
        known = ({r["raw_id"] for r in proj.measurements.values()}
                 | {r["raw_id"] for r in proj.returns.values()}
                 | {r["raw_id"] for r in proj.inflows.values()})
        if raw_id not in known:
            raise ServiceError(f"更正记录引用了不存在的原始记录: {raw_id}")

    def event(self, seq: int) -> dict[str, Any] | None:
        event = self.ledger.get(seq)
        return event.to_dict() if event else None

    # ------------------------------------------------------------- 基础档案
    def register_permit(self, **payload: Any) -> PostResult:
        payload.setdefault("use_codes", list(payload.get("use_codes", [])))
        return self._post("permit_registered", payload)

    def revise_permit(self, **payload: Any) -> PostResult:
        self._require_permit(payload["permit_id"])
        return self._post("permit_revised", payload)

    def suspend_permit(self, **payload: Any) -> PostResult:
        self._require_permit(payload["permit_id"])
        return self._post("permit_suspended", payload)

    def resume_permit(self, **payload: Any) -> PostResult:
        self._require_permit(payload["permit_id"])
        return self._post("permit_resumed", payload)

    def open_quota(self, **payload: Any) -> PostResult:
        self._require_hour_order(payload["start_hour"], payload["end_hour"])
        if float(payload["quota_m3"]) < 0:
            raise ServiceError("季节额度不能为负")
        self._require_permit(payload["permit_id"])
        return self._post("quota_opened", payload)

    def amend_quota(self, **payload: Any) -> PostResult:
        proj = self.projection()
        if payload["quota_id"] not in proj.quotas:
            raise ServiceError(f"季节额度账户不存在: {payload['quota_id']}")
        return self._post("quota_amended", payload)

    def register_section(self, **payload: Any) -> PostResult:
        return self._post("section_registered", payload)

    def schedule_section_requirement(self, **payload: Any) -> PostResult:
        self._require_hour_order(payload["start_hour"], payload["end_hour"])
        self._require_section(payload["section_id"])
        return self._post("section_requirement_scheduled", payload)

    def link_section_point(self, **payload: Any) -> PostResult:
        factor = float(payload["consumptive_factor"])
        if not 0 <= factor <= 1:
            raise ServiceError("耗水系数必须位于 [0, 1]")
        if int(payload["lag_hours"]) < 0:
            raise ServiceError("传播滞后不能为负")
        self._require_section(payload["section_id"])
        return self._post("section_linked", payload)

    @staticmethod
    def _require_hour_order(start: str, end: str) -> None:
        if parse_hour(end) <= parse_hour(start):
            raise ServiceError("结束小时必须晚于开始小时")

    # ----------------------------------------------------------- 计量与退水
    def record_measurement(self, raw_id: str, permit_id: str, event_hour: str,
                           use_code: str, gross_m3: float, source: str,
                           observed_month: str | None = None,
                           estimate: bool = False) -> PostResult:
        if float(gross_m3) < 0:
            raise ServiceError("计量水量不能为负")
        if estimate and not source.strip():
            raise ServiceError("暂估计量必须注明明确来源")
        self._require_permit(permit_id)
        return self._post("measurement_recorded", {
            "raw_id": raw_id, "permit_id": permit_id, "event_hour": event_hour,
            "use_code": use_code, "gross_m3": float(gross_m3),
            "estimate": bool(estimate), "source": source,
            "observed_month": observed_month or month_of(event_hour),
        })

    def amend_measurement(self, amendment_id: str, raw_id: str, delta_m3: float,
                          kind: str, reason: str, source: str,
                          observed_month: str) -> PostResult:
        """补报/更正：只登记差异。补报把暂估替换为实测口径。"""
        if kind not in {"correction", "backfill"}:
            raise ServiceError("kind 必须是 correction 或 backfill")
        self._require_raw(self.projection(), raw_id)
        return self._post("measurement_amended", {
            "amendment_id": amendment_id, "raw_id": raw_id,
            "delta_m3": float(delta_m3), "kind": kind, "reason": reason,
            "source": source, "observed_month": observed_month,
        })

    def record_return(self, raw_id: str, permit_id: str, event_hour: str,
                      return_m3: float, source: str,
                      observed_month: str | None = None,
                      estimate: bool = False) -> PostResult:
        if float(return_m3) < 0:
            raise ServiceError("退水量不能为负")
        if estimate and not source.strip():
            raise ServiceError("暂估退水必须注明明确来源")
        self._require_permit(permit_id)
        return self._post("return_recorded", {
            "raw_id": raw_id, "permit_id": permit_id, "event_hour": event_hour,
            "return_m3": float(return_m3), "estimate": bool(estimate),
            "source": source,
            "observed_month": observed_month or month_of(event_hour),
        })

    def amend_return(self, amendment_id: str, raw_id: str, delta_m3: float,
                     kind: str, reason: str, source: str,
                     observed_month: str) -> PostResult:
        self._require_raw(self.projection(), raw_id)
        return self._post("return_amended", {
            "amendment_id": amendment_id, "raw_id": raw_id,
            "delta_m3": float(delta_m3), "kind": kind, "reason": reason,
            "source": source, "observed_month": observed_month,
        })

    def record_inflow(self, raw_id: str, section_id: str, event_hour: str,
                      inflow_m3: float, source: str,
                      observed_month: str | None = None,
                      estimate: bool = False) -> PostResult:
        self._require_section(section_id)
        return self._post("inflow_recorded", {
            "raw_id": raw_id, "section_id": section_id, "event_hour": event_hour,
            "inflow_m3": float(inflow_m3), "estimate": bool(estimate),
            "source": source,
            "observed_month": observed_month or month_of(event_hour),
        })

    def amend_inflow(self, amendment_id: str, raw_id: str, delta_m3: float,
                     kind: str, reason: str, source: str,
                     observed_month: str) -> PostResult:
        self._require_raw(self.projection(), raw_id)
        return self._post("inflow_amended", {
            "amendment_id": amendment_id, "raw_id": raw_id,
            "delta_m3": float(delta_m3), "kind": kind, "reason": reason,
            "source": source, "observed_month": observed_month,
        })

    # ----------------------------------------------------------------- 调剂
    def approve_transfer(self, transfer_id: str, from_permit: str, to_permit: str,
                         event_hour: str, use_code: str, amount_m3: float,
                         doc_ref: str, observed_month: str | None = None) -> PostResult:
        if float(amount_m3) <= 0:
            raise ServiceError("调剂水量必须为正")
        if from_permit == to_permit:
            raise ServiceError("调剂双方不能相同")
        proj = self._require_permit(from_permit)
        if to_permit not in proj.permits:
            raise ServiceError(f"调入方许可不存在: {to_permit}")
        return self._post("transfer_approved", {
            "transfer_id": transfer_id, "from_permit": from_permit,
            "to_permit": to_permit, "event_hour": event_hour,
            "use_code": use_code, "amount_m3": float(amount_m3),
            "doc_ref": doc_ref,
            "observed_month": observed_month or month_of(event_hour),
        })

    # --------------------------------------------------- 临时申请：先评估后裁定
    def evaluate_application(self, permit_id: str, use_code: str, start_hour: str,
                             end_hour: str, requested_m3_per_hour: float,
                             priority_flag: bool = False) -> dict[str, Any]:
        """逐时评估申请对许可约束与下游生态断面的影响，但不写入任何事件。"""
        proj = self.projection()
        if permit_id not in proj.permits:
            raise ServiceError(f"许可不存在: {permit_id}")
        rate = float(requested_m3_per_hour)
        if rate <= 0:
            raise ServiceError("申请流量必须为正")
        self._require_hour_order(start_hour, end_hour)
        hours = hours_between(start_hour, end_hour)
        violations: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        # 优先保障对象（许可优先序为 1 或申请明确标注）在季节额度紧张时可优先占用，
        # 但生态流量、小时限值、用途授权、暂停状态仍是不可突破的硬约束。
        first_revision = proj.permit_revision_at(permit_id, start_hour)
        priority = bool(priority_flag) or int(first_revision["priority_order"]) <= 1

        # 1) 用途、暂停状态、小时限值逐时检查
        for hour in hours:
            revision = proj.permit_revision_at(permit_id, hour)
            if use_code not in revision["use_codes"]:
                violations.append({"hour": hour, "rule": "use_not_authorized",
                                   "authorized": revision["use_codes"],
                                   "evidence": [revision["seq"]]})
                continue
            if proj.is_suspended(permit_id, hour) is not None:
                interval = proj.is_suspended(permit_id, hour)
                violations.append({"hour": hour, "rule": "permit_suspended",
                                   "evidence": [interval["seq"]]})
                continue
            current = proj.withdrawal(permit_id, hour, use_code)
            projected = current.value + rate
            if (revision["hourly_limit_m3"] is not None
                    and projected > float(revision["hourly_limit_m3"])):
                violations.append({
                    "hour": hour, "rule": "hourly_limit_exceeded",
                    "current_m3": current.value, "projected_m3": projected,
                    "limit_m3": revision["hourly_limit_m3"],
                    "evidence": current.evidence + [revision["seq"]],
                })

        # 2) 季节额度：申请窗口落在哪些季节额度内
        quota_checks = []
        total_request = rate * len(hours)
        for quota in proj.quotas_for(permit_id, use_code):
            overlap_start = max(quota["start_hour"], start_hour)
            overlap_end = min(quota["end_hour"], end_hour)
            if overlap_start >= overlap_end:
                continue
            overlap = hours_between(overlap_start, overlap_end)
            # 用量截至申请窗口末端与季节额度末端的较早者，避免跨季漏算或越界。
            used_through = min(add_hours(end_hour, -1),
                               add_hours(quota["end_hour"], -1))
            used = proj.quota_used_through(quota, used_through)
            budget = proj.quota_budget_at(quota, end_hour)
            request_share = rate * len(overlap)
            remaining = budget - used.value
            quota_checks.append({
                "quota_id": quota["quota_id"], "budget_m3": budget,
                "used_m3": used.value, "remaining_m3": remaining,
                "requested_m3": request_share,
                "projected_remaining_m3": remaining - request_share,
                "state": "overdraft" if request_share > remaining else "compliant",
                "evidence": used.evidence,
            })
            if request_share > remaining:
                finding = {
                    "hour": overlap[-1], "rule": "seasonal_quota_exceeded",
                    "quota_id": quota["quota_id"],
                    "remaining_m3": remaining, "requested_m3": request_share,
                    "evidence": used.evidence,
                }
                if priority:
                    # 优先保障对象：允许先行占用并提示后续统筹，不作否决。
                    warnings.append({**finding, "rule": "seasonal_quota_priority_overdraw"})
                else:
                    violations.append(finding)

        # 3) 下游生态控制断面：逐时平衡，按滞后与耗水系数折算
        point = proj.point_of(permit_id)
        section_checks = []
        seen_section_hours: set[tuple[str, str]] = set()
        for link in proj.links:
            if link["point"] != point:
                continue
            section_id = link["section_id"]
            extra_depletion: dict[str, dict[str, str]] = {point: {}}
            per_hour = extra_depletion[point]
            # 申请取水发生在 source_hour，断面影响推迟 lag_hours。
            for hour in hours:
                per_hour[hour] = per_hour.get(hour, 0.0) \
                    + rate * link["consumptive_factor"]
            for hour in hours:
                impact_hour = add_hours(hour, link["lag_hours"])
                key = (section_id, impact_hour)
                if key in seen_section_hours:
                    continue
                seen_section_hours.add(key)
                balance = proj.section_balance(section_id, impact_hour, extra_depletion)
                if balance["inflow_m3"] is None:
                    check_state = "inflow_data_missing"
                else:
                    check_state = balance["state"]
                section_checks.append({
                    "section_id": section_id, "hour": impact_hour,
                    "state": check_state,
                    "balance_m3": balance["balance_m3"],
                    "required_m3": balance["required_m3"],
                    "margin_m3": (None if balance["balance_m3"] is None
                                  else balance["balance_m3"] - balance["required_m3"]),
                    "evidence": balance["evidence"],
                })
                if balance["balance_m3"] is not None \
                        and balance["balance_m3"] < balance["required_m3"]:
                    violations.append({
                        "hour": impact_hour, "rule": "eco_flow_breach",
                        "section_id": section_id,
                        "balance_m3": balance["balance_m3"],
                        "required_m3": balance["required_m3"],
                        "evidence": balance["evidence"],
                    })

        decision = "approved" if not violations else "rejected"
        return {
            "permit_id": permit_id, "use_code": use_code,
            "start_hour": start_hour, "end_hour": end_hour,
            "requested_m3_per_hour": rate, "priority_flag": priority,
            "total_requested_m3": total_request,
            "decision": decision, "violations": violations,
            "warnings": warnings,
            "quota_checks": quota_checks, "section_checks": section_checks,
        }

    def submit_application(self, application_id: str, permit_id: str, use_code: str,
                           start_hour: str, end_hour: str,
                           requested_m3_per_hour: float,
                           priority_flag: bool = False,
                           observed_month: str | None = None,
                           auto_decide: bool = True) -> dict[str, Any]:
        """登记申请；默认立即按评估结论裁定，并把逐时依据一并留痕。"""
        # 先评估验证，避免对不存在/不合规申请留下无裁定的悬挂事件。
        assessment = self.evaluate_application(
            permit_id, use_code, start_hour, end_hour,
            requested_m3_per_hour, priority_flag)
        result = self._post("application_submitted", {
            "application_id": application_id, "permit_id": permit_id,
            "use_code": use_code, "start_hour": start_hour, "end_hour": end_hour,
            "requested_m3_per_hour": float(requested_m3_per_hour),
            "priority_flag": bool(priority_flag),
            "observed_month": observed_month or month_of(start_hour),
        })
        # 提交事件不改变评估口径，复用提交前评估结果即可。
        if auto_decide and not result.duplicate:
            self.decide_application(
                application_id, assessment["decision"],
                "自动裁定：" + ("未发现约束冲突" if assessment["decision"] == "approved"
                            else "存在逐时约束冲突"),
                observed_month or month_of(start_hour),
                basis=assessment)
        return {"submit": result.__dict__, "assessment": assessment,
                "duplicate": result.duplicate}

    def decide_application(self, application_id: str, decision: str, reason: str,
                           observed_month: str, basis: dict[str, Any] | None = None) -> PostResult:
        proj = self.projection()
        if application_id not in proj.applications:
            raise ServiceError(f"申请不存在: {application_id}")
        if proj.applications[application_id]["decision"] is not None:
            raise ServiceError(f"申请 {application_id} 已有裁定，不得更改")
        return self._post("application_decided", {
            "application_id": application_id, "decision": decision,
            "reason": reason, "basis": basis,
            "observed_month": observed_month,
        })

    # ----------------------------------------------------------------- 月报
    STRUCTURAL_EVENTS = {
        "permit_registered", "permit_revised", "permit_suspended",
        "permit_resumed", "quota_opened", "quota_amended",
        "section_registered", "section_requirement_scheduled", "section_linked",
    }

    @staticmethod
    def _event_posting_month(event: Event) -> str:
        return str(event.payload.get("observed_month")
                   or event.payload.get("month")
                   or month_of(str(event.payload.get("event_hour", "9999-01"))))

    def _report_events(self, month: str, through_seq: int) -> list[Event]:
        """签署口径：截至签署序号的结构事件 + 当月（含）以前的运行事件。

        用 ``through_seq`` 冻结结构档案，使得签署后补登记的许可修订不会改变
        旧月报的复算指纹；旧报告仍可被逐位复现。
        """
        scoped: list[Event] = []
        for event in self.ledger.read_since(0):
            if event.seq is not None and event.seq > through_seq:
                break
            if event.event_type in self.STRUCTURAL_EVENTS:
                scoped.append(event)
            elif event.event_type != "report_signed" \
                    and self._event_posting_month(event) <= month:
                scoped.append(event)
        return scoped

    def build_monthly_report(self, month: str, through_seq: int | None = None) -> dict[str, Any]:
        """按签署口径构造月报快照。

        快照列出全部依据事件的 seq 与指纹，任何篡改都会改变报告指纹。
        """
        if through_seq is None:
            through_seq = self.ledger.latest_seq()
        included = self._report_events(month, through_seq)
        proj = Projection(included)
        start = f"{month}-01T00"
        return self._report_snapshot(month, proj, included, start)

    def _report_snapshot(self, month: str, proj: Projection,
                         included: list[Event], start: str) -> dict[str, Any]:
        base = datetime.strptime(f"{month}-01", "%Y-%m-%d")
        if base.month == 12:
            end_dt = base.replace(year=base.year + 1, month=1)
        else:
            end_dt = base.replace(month=base.month + 1)
        end = end_dt.strftime("%Y-%m-%dT%H")

        permits_out = {}
        for permit_id, permit in sorted(proj.permits.items()):
            withdrawn_m3 = 0.0
            returned_m3 = 0.0
            estimated_hours: list[str] = []
            evidence: set[int] = set()
            for (pid, hour, _use), row in proj.measurements.items():
                if pid == permit_id and hour[:7] == month:
                    withdrawn_m3 += row["net"]
                    if row["estimate"]:
                        estimated_hours.append(hour)
                    evidence.add(row["base"]["seq"])
                    for amendment in row["amendments"]:
                        evidence.add(amendment["seq"])
            for (pid, hour), row in proj.returns.items():
                if pid == permit_id and hour[:7] == month:
                    returned_m3 += row["net"]
                    evidence.add(row["base"]["seq"])
            quotas = []
            for quota in proj.quotas.values():
                if quota["permit_id"] != permit_id:
                    continue
                if not (quota["start_hour"] < end and quota["end_hour"] > start):
                    continue
                last = max(h for h in hours_between(start, end)
                           if h < quota["end_hour"])
                used = proj.quota_used_through(quota, last)
                budget = proj.quota_budget_at(quota, last)
                quotas.append({"quota_id": quota["quota_id"],
                               "use_code": quota["use_code"],
                               "season_label": quota["season_label"],
                               "budget_m3": budget, "used_m3": used.value,
                               "remaining_m3": budget - used.value,
                               "evidence": used.evidence})
            alerts = [i for i in proj.alert_intervals(start, end)["permits"]
                      .get(permit_id, [])
                      if i["state"] in {"overdraft", "near_breach"}
                      and i["start_hour"][:7] == month]
            permits_out[permit_id] = {
                "subject_name": permit["subject_name"], "point": permit["point"],
                "withdrawn_m3": withdrawn_m3, "returned_m3": returned_m3,
                "net_depletion_m3": withdrawn_m3 - returned_m3,
                "estimated_hours": sorted(estimated_hours),
                "quotas": quotas, "alerts": alerts,
            }

        sections_out = {}
        for section_id in sorted(proj.sections):
            hourly = proj.section_hourly(section_id, start, end)
            breaches = [h for h in hourly if h["state"] == "breach"]
            near = [h for h in hourly if h["state"] == "near_breach"]
            sections_out[section_id] = {
                "breach_hours": [{"hour": h["hour"], "balance_m3": h["balance_m3"],
                                  "required_m3": h["required_m3"],
                                  "evidence": h["evidence"]} for h in breaches],
                "near_breach_hours": [h["hour"] for h in near],
                "data_missing_hours": [h["hour"] for h in hourly
                                       if h["state"] == "data_missing"],
            }

        applications = []
        for app in proj.applications.values():
            if app["observed_month"] != month:
                continue
            applications.append({
                "application_id": app["application_id"],
                "permit_id": app["permit_id"], "use_code": app["use_code"],
                "decision": app["decision"]["decision"] if app["decision"] else None,
                "evidence": [app["submit_seq"]]
                            + ([app["decision"]["seq"]] if app["decision"] else []),
            })

        basis = sorted(
            ({"seq": e.seq, "event_type": e.event_type,
              "fingerprint": e.fingerprint} for e in included if e.seq is not None),
            key=lambda r: r["seq"],
        )
        snapshot = {
            "month": month,
            "permits": permits_out,
            "sections": sections_out,
            "applications": applications,
            "basis_events": basis,
        }
        snapshot["fingerprint"] = sha256(
            canon_json({k: v for k, v in snapshot.items()}).encode("utf-8")
        ).hexdigest()
        return snapshot

    def sign_report(self, month: str, signed_by: str, doc_ref: str) -> PostResult:
        """签署月报：快照与指纹固化进台账，同月不可重复签署或改写。"""
        proj = self.projection()
        if month in proj.reports:
            raise ServiceError(f"{month} 月报已签署，不得重新签署或改写")
        through_seq = self.ledger.latest_seq()
        snapshot = self.build_monthly_report(month, through_seq=through_seq)
        snapshot["ledger_seq"] = through_seq
        # ledger_seq 不参与指纹（它是定位信息），指纹在 build 中已计算。
        return self._post("report_signed", {
            "month": month, "fingerprint": snapshot["fingerprint"],
            "signed_by": signed_by, "doc_ref": doc_ref,
            "snapshot": snapshot,
        })

    def verify_report(self, month: str) -> dict[str, Any]:
        """按签署时口径复算并验签；列出签署后到达的差异调整。"""
        proj = self.projection()
        if month not in proj.reports:
            raise ServiceError(f"{month} 月报尚未签署")
        signed = proj.reports[month]
        through_seq = int(signed["snapshot"].get("ledger_seq", signed["seq"]))
        recomputed = self.build_monthly_report(month, through_seq=through_seq)
        # raw_id -> 物理小时，使补报差异能归集到被补报的时段。
        raw_hours: dict[str, str] = {}
        for event in self.ledger.all_events():
            if event.event_type in {"measurement_recorded", "return_recorded",
                                    "inflow_recorded"}:
                raw_hours[event.payload["raw_id"]] = event.payload["event_hour"]
        later_adjustments = []
        for event in self.ledger.all_events():
            if (event.seq or 0) <= through_seq:
                continue
            payload = event.payload
            if event.event_type in {"measurement_amended", "return_amended",
                                    "inflow_amended"}:
                phys_hour = raw_hours.get(payload.get("raw_id", ""), "")
            elif event.event_type in {"measurement_recorded", "return_recorded",
                                      "inflow_recorded", "transfer_approved"}:
                phys_hour = payload.get("event_hour", "")
            else:
                continue
            if phys_hour[:7] == month:
                later_adjustments.append(event.to_dict())
        return {
            "month": month,
            "stored_fingerprint": signed["fingerprint"],
            "recomputed_fingerprint": recomputed["fingerprint"],
            "intact": signed["fingerprint"] == recomputed["fingerprint"],
            "signed_by": signed["signed_by"], "signed_seq": signed["seq"],
            "post_signing_adjustments": later_adjustments,
        }

    # ------------------------------------------------------- 执法复核与申诉
    def record_enforcement(self, **payload: Any) -> PostResult:
        proj = self.projection()
        if payload["scope_type"] == "permit" and payload["target"] not in proj.permits:
            raise ServiceError(f"执法对象许可不存在: {payload['target']}")
        if payload["scope_type"] == "section" and payload["target"] not in proj.sections:
            raise ServiceError(f"执法对象断面不存在: {payload['target']}")
        related = payload.get("related_case")
        if related is not None and not any(
                e["case_id"] == related for e in proj.enforcement):
            raise ServiceError(f"申诉引用的原案件不存在: {related}")
        return self._post("enforcement_recorded", payload)

    # ----------------------------------------------------------------- 复算
    def availability(self, permit_id: str, use_code: str, hour: str) -> dict[str, Any]:
        """任一时点的可用量：季节额度余量、小时限值余量与依据记录。"""
        proj = self.projection()
        if permit_id not in proj.permits:
            raise ServiceError(f"许可不存在: {permit_id}")
        revision = proj.permit_revision_at(permit_id, hour)
        suspended = proj.is_suspended(permit_id, hour)
        used_hour = proj.withdrawal(permit_id, hour, use_code)
        limit = revision["hourly_limit_m3"]
        quota_rows = []
        for quota in proj.quotas_for(permit_id, use_code):
            if not (quota["start_hour"] <= hour < quota["end_hour"]):
                continue
            used = proj.quota_used_through(quota, hour)
            budget = proj.quota_budget_at(quota, hour)
            quota_rows.append({
                "quota_id": quota["quota_id"], "season_label": quota["season_label"],
                "budget_m3": budget, "used_m3": used.value,
                "remaining_m3": budget - used.value,
                "estimate": used.estimate, "evidence": used.evidence,
            })
        return {
            "permit_id": permit_id, "use_code": use_code, "hour": hour,
            "authorized": use_code in revision["use_codes"],
            "suspended": suspended is not None,
            "hourly_limit_m3": limit,
            "used_this_hour_m3": used_hour.value,
            "hourly_headroom_m3": (None if limit is None
                                   else float(limit) - used_hour.value),
            "quotas": quota_rows,
            "evidence": sorted(set(
                used_hour.evidence
                + [seq for row in quota_rows for seq in row["evidence"]]
                + [revision["seq"]]
            )),
        }

    def compliance(self, start_hour: str, end_hour: str) -> dict[str, Any]:
        """复算窗口内每个许可与断面的逐时履约、区间识别与证据链。"""
        self._require_hour_order(start_hour, end_hour)
        proj = self.projection()
        intervals = proj.alert_intervals(start_hour, end_hour)
        return {
            "window": {"start_hour": start_hour, "end_hour": end_hour},
            "permits": {
                pid: {
                    "point": proj.permits[pid]["point"],
                    "subject_name": proj.permits[pid]["subject_name"],
                    "hourly": proj.permit_hourly(pid, start_hour, end_hour),
                    "intervals": intervals["permits"][pid],
                } for pid in sorted(proj.permits)
            },
            "sections": {
                sid: {
                    "hourly": proj.section_hourly(sid, start_hour, end_hour),
                    "intervals": intervals["sections"][sid],
                } for sid in sorted(proj.sections)
            },
        }

    def alert_basis(self, start_hour: str, end_hour: str) -> dict[str, Any]:
        """只输出告警/恢复区间及其所依据的原始记录（含暂估标记与来源）。"""
        report = self.compliance(start_hour, end_hour)
        proj = self.projection()

        def expand(seqs: list[int]) -> list[dict[str, Any]]:
            out = []
            for seq in sorted(set(seqs)):
                event = proj.events_by_seq.get(seq)
                if event is not None:
                    out.append(event.to_dict())
            return out

        result = {"window": report["window"], "permits": {}, "sections": {}}
        for pid, body in report["permits"].items():
            flagged = [i for i in body["intervals"]
                       if i["state"] in {"overdraft", "near_breach"}
                       or i.get("recovered")]
            result["permits"][pid] = [{
                "state": i["state"], "start_hour": i["start_hour"],
                "end_hour": i["end_hour"], "recovered": i.get("recovered", False),
                "prior_state": i.get("prior_state"),
                "basis_records": expand(i["evidence"]),
            } for i in flagged]
        for sid, body in report["sections"].items():
            flagged = [i for i in body["intervals"]
                       if i["state"] in {"breach", "near_breach", "data_missing"}
                       or i.get("recovered")]
            result["sections"][sid] = [{
                "state": i["state"], "start_hour": i["start_hour"],
                "end_hour": i["end_hour"], "recovered": i.get("recovered", False),
                "prior_state": i.get("prior_state"),
                "basis_records": expand(i["evidence"]),
            } for i in flagged]
        return result
