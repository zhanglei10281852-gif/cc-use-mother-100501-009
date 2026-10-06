"""只读投影：把只追加台账重放为可查询的履约状态。

投影不保存“权威状态”——任何结论都可以通过 ``Projection(events)`` 重新推导。
每个逐时结论都携带来源事件 ``seq`` 列表，满足“告警必须能追溯到所依据的记录”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .events import Event
from .timebuckets import add_hours, hours_between

# 余额低于额度/生态要求的该比例时判为“即将违约”。
NEAR_RATIO = 0.10


@dataclass
class Trace:
    """一个逐量值及其证据链。"""

    value: float
    estimate: bool
    evidence: list[int] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


class Projection:
    def __init__(self, events: Iterable[Event]):
        events = sorted(events, key=lambda e: e.seq or 0)
        self.permits: dict[str, dict[str, Any]] = {}
        self.quotas: dict[str, dict[str, Any]] = {}
        self.sections: dict[str, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []
        # (permit, hour, use) -> 汇总计量
        self.measurements: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (permit, hour) -> 汇总退水
        self.returns: dict[tuple[str, str], dict[str, Any]] = {}
        # (section, hour) -> 汇总入库
        self.inflows: dict[tuple[str, str], dict[str, Any]] = {}
        # (permit, hour, use) -> [transfer...]
        self.transfers: list[dict[str, Any]] = []
        self.applications: dict[str, dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.enforcement: list[dict[str, Any]] = []
        self.events_by_seq: dict[int, Event] = {}
        for event in events:
            self.events_by_seq[event.seq or 0] = event
            self._apply(event)

    # ------------------------------------------------------------------ 重放
    def _apply(self, event: Event) -> None:
        seq = event.seq or 0
        p = event.payload
        t = event.event_type
        if t == "permit_registered":
            if p["permit_id"] in self.permits:
                raise ValueError(f"许可 {p['permit_id']} 重复注册")
            self.permits[p["permit_id"]] = {
                "permit_id": p["permit_id"], "point": p["point"],
                "subject_id": p["subject_id"], "subject_name": p["subject_name"],
                "registered_at": p["effective_from"],
                "revisions": [{
                    "seq": seq, "valid_from": p["effective_from"],
                    "use_codes": list(p["use_codes"]),
                    "priority_order": p["priority_order"],
                    "hourly_limit_m3": p.get("hourly_limit_m3"),
                    "doc_ref": p["doc_ref"],
                }],
                "suspensions": [],
            }
        elif t == "permit_revised":
            permit = self.permits[p["permit_id"]]
            permit["revisions"].append({
                "seq": seq, "valid_from": p["valid_from"],
                "use_codes": list(p["use_codes"]),
                "priority_order": p["priority_order"],
                "hourly_limit_m3": p.get("hourly_limit_m3"),
                "doc_ref": p["doc_ref"],
            })
        elif t == "permit_suspended":
            permit = self.permits[p["permit_id"]]
            permit["suspensions"].append({
                "seq": seq, "start": p["from_hour"], "end": p.get("to_hour"),
                "reason": p["reason"], "doc_ref": p["doc_ref"],
            })
        elif t == "permit_resumed":
            permit = self.permits[p["permit_id"]]
            for interval in reversed(permit["suspensions"]):
                if interval["end"] is None:
                    interval["end"] = p["from_hour"]
                    interval["resume_seq"] = seq
                    break
        elif t == "quota_opened":
            if p["quota_id"] in self.quotas:
                raise ValueError(f"额度账户 {p['quota_id']} 重复开立")
            self.quotas[p["quota_id"]] = {
                "quota_id": p["quota_id"], "permit_id": p["permit_id"],
                "use_code": p["use_code"], "season_label": p["season_label"],
                "start_hour": p["start_hour"], "end_hour": p["end_hour"],
                "base_m3": float(p["quota_m3"]),
                "amendments": [], "seq": seq,
            }
        elif t == "quota_amended":
            self.quotas[p["quota_id"]]["amendments"].append({
                "seq": seq, "effective_hour": p["effective_hour"],
                "delta_m3": float(p["delta_m3"]),
                "reason": p["reason"], "doc_ref": p["doc_ref"],
            })
        elif t == "section_registered":
            self.sections[p["section_id"]] = {
                "section_id": p["section_id"], "name": p["name"],
                "eco_flow_m3": float(p["eco_flow_m3"]),
                "schedules": [], "seq": seq,
            }
        elif t == "section_requirement_scheduled":
            self.sections[p["section_id"]]["schedules"].append({
                "seq": seq, "start_hour": p["start_hour"], "end_hour": p["end_hour"],
                "eco_flow_m3": float(p["eco_flow_m3"]), "doc_ref": p["doc_ref"],
            })
        elif t == "section_linked":
            self.links.append({
                "seq": seq, "section_id": p["section_id"], "point": p["point"],
                "lag_hours": int(p["lag_hours"]),
                "consumptive_factor": float(p["consumptive_factor"]),
                "doc_ref": p["doc_ref"],
            })
        elif t in {"measurement_recorded", "return_recorded", "inflow_recorded"}:
            self._apply_series_base(t, seq, p)
        elif t in {"measurement_amended", "return_amended", "inflow_amended"}:
            self._apply_series_amend(t, seq, p)
        elif t == "transfer_approved":
            self.transfers.append({
                "seq": seq, "transfer_id": p["transfer_id"],
                "from_permit": p["from_permit"], "to_permit": p["to_permit"],
                "hour": p["event_hour"], "use_code": p["use_code"],
                "amount_m3": float(p["amount_m3"]),
                "doc_ref": p["doc_ref"], "observed_month": p["observed_month"],
            })
        elif t == "application_submitted":
            self.applications[p["application_id"]] = {
                "application_id": p["application_id"], "permit_id": p["permit_id"],
                "use_code": p["use_code"], "start_hour": p["start_hour"],
                "end_hour": p["end_hour"],
                "rate_m3_per_hour": float(p["requested_m3_per_hour"]),
                "priority_flag": bool(p["priority_flag"]),
                "observed_month": p["observed_month"],
                "submit_seq": seq, "decision": None,
            }
        elif t == "application_decided":
            app = self.applications[p["application_id"]]
            app["decision"] = {
                "seq": seq, "decision": p["decision"], "reason": p["reason"],
                "basis": p.get("basis"), "observed_month": p["observed_month"],
            }
        elif t == "report_signed":
            self.reports[p["month"]] = {
                "month": p["month"], "fingerprint": p["fingerprint"],
                "signed_by": p["signed_by"], "seq": seq,
                "doc_ref": p["doc_ref"], "snapshot": p["snapshot"],
            }
        elif t == "enforcement_recorded":
            self.enforcement.append({
                "seq": seq, "case_id": p["case_id"],
                "scope_type": p["scope_type"], "target": p["target"],
                "start_hour": p.get("start_hour"), "end_hour": p.get("end_hour"),
                "status": p["status"], "conclusion": p["conclusion"],
                "reviewer": p["reviewer"], "observed_month": p["observed_month"],
                "related_case": p.get("related_case"),
            })

    def _series_table(self, kind: str) -> dict[tuple, dict[str, Any]]:
        if kind.startswith("measurement"):
            return self.measurements
        if kind.startswith("return"):
            return self.returns
        return self.inflows

    @staticmethod
    def _series_key(kind: str, p: dict[str, Any]) -> tuple:
        if kind.startswith("measurement"):
            return (p["permit_id"], p["event_hour"], p["use_code"])
        if kind.startswith("return"):
            return (p["permit_id"], p["event_hour"])
        return (p["section_id"], p["event_hour"])

    @staticmethod
    def _series_value_field(kind: str) -> str:
        if kind.startswith("measurement"):
            return "gross_m3"
        if kind.startswith("return"):
            return "return_m3"
        return "inflow_m3"

    def _apply_series_base(self, kind: str, seq: int, p: dict[str, Any]) -> None:
        table = self._series_table(kind)
        key = self._series_key(kind, p)
        if key in table:
            raise ValueError(f"{kind} 同一业务键重复: {key}")
        table[key] = {
            "raw_id": p["raw_id"], "net": float(p[self._series_value_field(kind)]),
            "estimate": bool(p["estimate"]),
            "base": {"seq": seq, "value": float(p[self._series_value_field(kind)]),
                     "estimate": bool(p["estimate"]), "source": p["source"],
                     "observed_month": p["observed_month"]},
            "amendments": [],
        }

    def _apply_series_amend(self, kind: str, seq: int, p: dict[str, Any]) -> None:
        base_kind = kind.split("_")[0]
        table = self._series_table(base_kind)
        target = None
        for key, row in table.items():
            if row["raw_id"] == p["raw_id"]:
                target = row
                break
        if target is None:
            raise ValueError(f"更正记录引用了不存在的原始记录 {p['raw_id']}")
        delta = float(p["delta_m3"])
        target["net"] += delta
        # 任何补报/更正都把暂估替换为实测口径。
        target["estimate"] = False
        target["amendments"].append({
            "seq": seq, "delta_m3": delta, "kind": p["kind"],
            "reason": p["reason"], "source": p["source"],
            "observed_month": p["observed_month"],
        })

    # ----------------------------------------------------------- 许可版本口径
    def permit_revision_at(self, permit_id: str, hour: str) -> dict[str, Any]:
        permit = self.permits[permit_id]
        current = permit["revisions"][0]
        for revision in sorted(permit["revisions"], key=lambda r: r["valid_from"]):
            if revision["valid_from"] <= hour:
                current = revision
        return current

    def is_suspended(self, permit_id: str, hour: str) -> dict[str, Any] | None:
        for interval in self.permits[permit_id]["suspensions"]:
            if interval["start"] <= hour and (interval["end"] is None or hour < interval["end"]):
                return interval
        return None

    def point_of(self, permit_id: str) -> str:
        return self.permits[permit_id]["point"]

    def permits_at_point(self, point: str) -> list[str]:
        return [pid for pid, permit in self.permits.items() if permit["point"] == point]

    # --------------------------------------------------------------- 逐时取数
    def _trace(self, row: dict[str, Any] | None) -> Trace | None:
        if row is None:
            return None
        records = [dict(row["base"], kind="base")] + [dict(a) for a in row["amendments"]]
        return Trace(value=row["net"], estimate=row["estimate"],
                     evidence=[r["seq"] for r in records], records=records)

    def withdrawal(self, permit_id: str, hour: str, use_code: str | None = None) -> Trace:
        if use_code is not None:
            trace = self._trace(self.measurements.get((permit_id, hour, use_code)))
            return trace or Trace(0.0, False, [], [])
        total = Trace(0.0, False, [], [])
        for (pid, h, _use), row in self.measurements.items():
            if pid == permit_id and h == hour:
                subtotal = self._trace(row)
                assert subtotal is not None
                total.value += subtotal.value
                total.estimate = total.estimate or subtotal.estimate
                total.evidence.extend(subtotal.evidence)
                total.records.extend(subtotal.records)
        return total

    def return_flow(self, permit_id: str, hour: str) -> Trace:
        trace = self._trace(self.returns.get((permit_id, hour)))
        return trace or Trace(0.0, False, [], [])

    def inflow(self, section_id: str, hour: str) -> Trace | None:
        return self._trace(self.inflows.get((section_id, hour)))

    # ------------------------------------------------------------- 季节额度账
    def quotas_for(self, permit_id: str, use_code: str) -> list[dict[str, Any]]:
        return [q for q in self.quotas.values()
                if q["permit_id"] == permit_id and q["use_code"] == use_code]

    def quota_budget_at(self, quota: dict[str, Any], hour: str) -> float:
        budget = quota["base_m3"]
        for amendment in quota["amendments"]:
            if amendment["effective_hour"] <= hour:
                budget += amendment["delta_m3"]
        for transfer in self.transfers:
            if transfer["hour"] > hour or transfer["use_code"] != quota["use_code"]:
                continue
            if transfer["from_permit"] == quota["permit_id"]:
                budget -= transfer["amount_m3"]
            elif transfer["to_permit"] == quota["permit_id"]:
                budget += transfer["amount_m3"]
        return budget

    def quota_used_through(self, quota: dict[str, Any], through_hour: str) -> Trace:
        """季节内截至 through_hour（含）的净取水。"""
        total = Trace(0.0, False, [], [])
        for (pid, hour, use), row in self.measurements.items():
            if (pid != quota["permit_id"] or use != quota["use_code"]
                    or not (quota["start_hour"] <= hour <= through_hour < quota["end_hour"])):
                continue
            subtotal = self._trace(row)
            assert subtotal is not None
            total.value += subtotal.value
            total.estimate = total.estimate or subtotal.estimate
            total.evidence.extend(subtotal.evidence)
            total.records.extend(subtotal.records)
        return total

    # ----------------------------------------------------------- 生态断面平衡
    def eco_requirement(self, section_id: str, hour: str) -> float:
        section = self.sections[section_id]
        required = section["eco_flow_m3"]
        for schedule in section["schedules"]:
            if schedule["start_hour"] <= hour < schedule["end_hour"]:
                required = schedule["eco_flow_m3"]
        return required

    def section_balance(
        self, section_id: str, hour: str,
        extra_depletion: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """计算断面某小时的平衡。

        历史实际平衡使用实测退水；``extra_depletion`` 用于申请预估，键为取水点，
        值为 {小时 -> 附加耗水量 m3}，按传播滞后平移。
        """
        inflow = self.inflow(section_id, hour)
        evidence: list[int] = []
        components: list[dict[str, Any]] = []
        if inflow is not None:
            evidence.extend(inflow.evidence)
            components.append({"kind": "inflow", "m3": inflow.value,
                               "estimate": inflow.estimate, "evidence": inflow.evidence})
        depletion = 0.0
        estimate = inflow.estimate if inflow is not None else False
        for link in self.links:
            if link["section_id"] != section_id:
                continue
            source_hour = add_hours(hour, -link["lag_hours"])
            point_depletion = 0.0
            for permit_id in self.permits_at_point(link["point"]):
                withdrawn = self.withdrawal(permit_id, source_hour)
                returned = self.return_flow(permit_id, source_hour)
                if returned.evidence:
                    # 有实测退水时，净耗水 = 取水 - 退水。
                    permit_depletion = withdrawn.value - returned.value
                elif withdrawn.evidence:
                    # 缺退水计量时按链接的耗水系数折算，并标注为估算。
                    permit_depletion = withdrawn.value * link["consumptive_factor"]
                else:
                    permit_depletion = 0.0
                point_depletion += permit_depletion
                evidence.extend(withdrawn.evidence)
                evidence.extend(returned.evidence)
                if withdrawn.evidence:
                    components.append({"kind": "withdrawal", "point": link["point"],
                                       "permit": permit_id, "hour": source_hour,
                                       "m3": withdrawn.value,
                                       "estimate": withdrawn.estimate,
                                       "evidence": withdrawn.evidence})
                if returned.evidence:
                    components.append({"kind": "return", "point": link["point"],
                                       "permit": permit_id, "hour": source_hour,
                                       "m3": returned.value,
                                       "estimate": returned.estimate,
                                       "evidence": returned.evidence})
                elif withdrawn.evidence:
                    components.append({"kind": "consumptive_estimate",
                                       "point": link["point"], "permit": permit_id,
                                       "hour": source_hour, "m3": permit_depletion,
                                       "estimate": True,
                                       "evidence": withdrawn.evidence + [link["seq"]]})
                    estimate = True
            extra = (extra_depletion or {}).get(link["point"], {}).get(source_hour)
            if extra:
                point_depletion += float(extra)
                components.append({"kind": "projected_depletion", "point": link["point"],
                                   "hour": source_hour, "m3": float(extra),
                                   "estimate": True, "evidence": [link["seq"]]})
                estimate = True
            depletion += point_depletion
        if inflow is None:
            balance: float | None = None
            state = "data_missing"
        else:
            balance = inflow.value - depletion
            required = self.eco_requirement(section_id, hour)
            if balance < required:
                state = "breach"
            elif balance < required * (1 + NEAR_RATIO):
                state = "near_breach"
            else:
                state = "compliant"
        return {
            "section_id": section_id, "hour": hour,
            "inflow_m3": inflow.value if inflow else None,
            "depletion_m3": depletion,
            "balance_m3": balance,
            "required_m3": self.eco_requirement(section_id, hour),
            "state": state, "estimate": estimate,
            "evidence": sorted(set(evidence)), "components": components,
        }

    def sections_downstream_of(self, point: str) -> list[str]:
        return sorted({l["section_id"] for l in self.links if l["point"] == point})

    # --------------------------------------------------------------- 区间识别
    @staticmethod
    def _intervals(hourly: list[dict[str, Any]], state_field: str) -> list[dict[str, Any]]:
        """把逐时状态压缩为连续区间。

        违约/预警区间之后出现的合规区间标记为 ``recovered``，使“恢复合规”成为
        一等结论；中间的缺测区间不打断恢复判定。
        """
        intervals: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for row in hourly:
            state = row[state_field]
            if current is None or current["state"] != state:
                if current is not None:
                    current["end_hour"] = row["hour"]
                    intervals.append(current)
                current = {"state": state, "start_hour": row["hour"],
                           "end_hour": add_hours(row["hour"], 1),
                           "hours": [], "evidence": set(), "recovered": False,
                           "prior_state": None}
            current["hours"].append(row["hour"])
            current["evidence"].update(row.get("evidence", []))
        if current is not None:
            intervals.append(current)
        recoverable_bad = {"breach", "near_breach", "overdraft"}
        previous_bad: str | None = None
        for interval in intervals:
            interval["evidence"] = sorted(interval["evidence"])
            if interval["state"] == "compliant":
                if previous_bad is not None:
                    interval["recovered"] = True
                    interval["prior_state"] = previous_bad
                previous_bad = None
            elif interval["state"] in recoverable_bad:
                previous_bad = interval["state"]
            # data_missing 等未知状态：保留 previous_bad，不认定恢复也不抹除前情
        return intervals

    def permit_hourly(self, permit_id: str, start: str, end: str) -> list[dict[str, Any]]:
        hourly: list[dict[str, Any]] = []
        quotas = {(q["use_code"], q["quota_id"]): q for q in self.quotas.values()
                  if q["permit_id"] == permit_id}
        for hour in hours_between(start, end):
            revision = self.permit_revision_at(permit_id, hour)
            suspended = self.is_suspended(permit_id, hour)
            withdrawn = self.withdrawal(permit_id, hour)
            reasons: list[str] = []
            evidence = list(withdrawn.evidence)
            if suspended and withdrawn.value > 0:
                reasons.append("withdrawal_while_suspended")
                evidence.append(suspended["seq"])
            # 用途越权：该小时出现了当前版本未授权的用途
            for (pid, h, use), row in self.measurements.items():
                if pid == permit_id and h == hour and use not in revision["use_codes"]:
                    reasons.append(f"use_not_authorized:{use}")
                    evidence.append(row["base"]["seq"])
            limit = revision["hourly_limit_m3"]
            over_limit = limit is not None and withdrawn.value > float(limit)
            if over_limit:
                reasons.append("hourly_limit_exceeded")
            # 季节额度累计口径
            quota_states = []
            for (use, qid), quota in quotas.items():
                if not (quota["start_hour"] <= hour < quota["end_hour"]):
                    continue
                used = self.quota_used_through(quota, hour)
                budget = self.quota_budget_at(quota, hour)
                remaining = budget - used.value
                if used.value > budget:
                    qstate = "overdraft"
                elif remaining <= max(0.0, budget) * NEAR_RATIO:
                    qstate = "near_breach"
                else:
                    qstate = "compliant"
                quota_states.append({
                    "quota_id": qid, "use_code": use,
                    "used_m3": used.value, "budget_m3": budget,
                    "remaining_m3": remaining, "state": qstate,
                    "estimate": used.estimate, "evidence": used.evidence,
                })
                evidence.extend(used.evidence)
            if any(q["state"] == "overdraft" for q in quota_states):
                quota_state = "overdraft"
            elif any(q["state"] == "near_breach" for q in quota_states):
                quota_state = "near_breach"
            else:
                quota_state = "compliant"
            if reasons:
                quota_state = "overdraft"
            hourly.append({
                "hour": hour, "withdrawal_m3": withdrawn.value,
                "estimate": withdrawn.estimate, "state": quota_state,
                "reasons": reasons, "quotas": quota_states,
                "suspended": suspended is not None,
                "evidence": sorted(set(evidence)),
            })
        return hourly

    def section_hourly(self, section_id: str, start: str, end: str,
                       extra_depletion: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
        return [self.section_balance(section_id, hour, extra_depletion)
                for hour in hours_between(start, end)]

    def alert_intervals(self, start: str, end: str) -> dict[str, Any]:
        """识别所有许可与断面在窗口内的违约/预警/恢复区间。"""
        result: dict[str, dict[str, Any]] = {"permits": {}, "sections": {}}
        for permit_id in sorted(self.permits):
            hourly = self.permit_hourly(permit_id, start, end)
            intervals = self._intervals(hourly, "state")
            result["permits"][permit_id] = intervals
        for section_id in sorted(self.sections):
            hourly = self.section_hourly(section_id, start, end)
            intervals = self._intervals(hourly, "state")
            result["sections"][section_id] = intervals
        return result

    def signed_months(self) -> set[str]:
        return set(self.reports)
