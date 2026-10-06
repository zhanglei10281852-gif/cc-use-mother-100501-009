"""只追加台账事件。

领域中的一切变化——许可版本、季节额度、逐时计量、退水、调剂、申请裁定、
月报签署、执法结论——都以不可变事件写入台账。事件只允许追加，更正以新事件
表达差异，因此任何历史结论都可以在日后被重新验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from .timebuckets import month_of, parse_hour

# 每种事件的负载字段。值为 (字段, 是否可空)。
FIELDS: dict[str, list[tuple[str, bool]]] = {
    # 取水点与许可版本
    "permit_registered": [
        ("permit_id", False), ("point", False), ("subject_id", False),
        ("subject_name", False), ("use_codes", False), ("priority_order", False),
        ("hourly_limit_m3", True), ("effective_from", False), ("doc_ref", False),
    ],
    "permit_revised": [
        ("permit_id", False), ("revision_seq", False), ("use_codes", False),
        ("priority_order", False), ("hourly_limit_m3", True),
        ("valid_from", False), ("doc_ref", False),
    ],
    "permit_suspended": [
        ("permit_id", False), ("from_hour", False), ("to_hour", True),
        ("reason", False), ("doc_ref", False),
    ],
    "permit_resumed": [
        ("permit_id", False), ("from_hour", False), ("reason", False), ("doc_ref", False),
    ],
    # 季节额度（按用途）
    "quota_opened": [
        ("quota_id", False), ("permit_id", False), ("use_code", False),
        ("season_label", False), ("start_hour", False), ("end_hour", False),
        ("quota_m3", False), ("doc_ref", False),
    ],
    "quota_amended": [
        ("amendment_id", False), ("quota_id", False), ("delta_m3", False),
        ("effective_hour", False), ("reason", False), ("doc_ref", False),
    ],
    # 下游生态控制断面
    "section_registered": [
        ("section_id", False), ("name", False), ("eco_flow_m3", False), ("doc_ref", False),
    ],
    "section_requirement_scheduled": [
        ("schedule_id", False), ("section_id", False), ("start_hour", False),
        ("end_hour", False), ("eco_flow_m3", False), ("doc_ref", False),
    ],
    "section_linked": [
        ("link_id", False), ("section_id", False), ("point", False),
        ("lag_hours", False), ("consumptive_factor", False), ("doc_ref", False),
    ],
    "inflow_recorded": [
        ("raw_id", False), ("section_id", False), ("event_hour", False),
        ("inflow_m3", False), ("estimate", False), ("source", False),
        ("observed_month", False),
    ],
    "inflow_amended": [
        ("amendment_id", False), ("raw_id", False), ("delta_m3", False),
        ("kind", False), ("reason", False), ("source", False), ("observed_month", False),
    ],
    # 逐时计量、退水（含暂估与更正）
    "measurement_recorded": [
        ("raw_id", False), ("permit_id", False), ("event_hour", False),
        ("use_code", False), ("gross_m3", False), ("estimate", False),
        ("source", False), ("observed_month", False),
    ],
    "measurement_amended": [
        ("amendment_id", False), ("raw_id", False), ("delta_m3", False),
        ("kind", False), ("reason", False), ("source", False), ("observed_month", False),
    ],
    "return_recorded": [
        ("raw_id", False), ("permit_id", False), ("event_hour", False),
        ("return_m3", False), ("estimate", False), ("source", False),
        ("observed_month", False),
    ],
    "return_amended": [
        ("amendment_id", False), ("raw_id", False), ("delta_m3", False),
        ("kind", False), ("reason", False), ("source", False), ("observed_month", False),
    ],
    # 经批准的跨主体调剂
    "transfer_approved": [
        ("transfer_id", False), ("from_permit", False), ("to_permit", False),
        ("event_hour", False), ("use_code", False), ("amount_m3", False),
        ("doc_ref", False), ("observed_month", False),
    ],
    # 临时增量申请与裁定
    "application_submitted": [
        ("application_id", False), ("permit_id", False), ("use_code", False),
        ("start_hour", False), ("end_hour", False), ("requested_m3_per_hour", False),
        ("priority_flag", False), ("observed_month", False),
    ],
    "application_decided": [
        ("application_id", False), ("decision", False), ("reason", False),
        ("basis", True), ("observed_month", False),
    ],
    # 已签署月报（只追加；快照内容一并固化）
    "report_signed": [
        ("month", False), ("fingerprint", False), ("signed_by", False),
        ("doc_ref", False), ("snapshot", False),
    ],
    # 执法复核与申诉结论
    "enforcement_recorded": [
        ("case_id", False), ("scope_type", False), ("target", False),
        ("start_hour", True), ("end_hour", True), ("status", False),
        ("conclusion", False), ("reviewer", False), ("observed_month", False),
        ("related_case", True),
    ],
}

# 各类业务标识使用的字段，供“原始记录重复导入不多扣”校验。
IDEMPOTENCY_FIELD = {
    "permit_registered": "permit_id",
    "permit_revised": "revision_seq",  # 与 permit_id 组合见 key()
    "permit_suspended": "from_hour",  # 与 permit_id 组合见 key()
    "permit_resumed": "from_hour",
    "quota_opened": "quota_id",
    "quota_amended": "amendment_id",
    "section_registered": "section_id",
    "section_requirement_scheduled": "schedule_id",
    "section_linked": "link_id",
    "inflow_recorded": "raw_id",
    "inflow_amended": "amendment_id",
    "measurement_recorded": "raw_id",
    "measurement_amended": "amendment_id",
    "return_recorded": "raw_id",
    "return_amended": "amendment_id",
    "transfer_approved": "transfer_id",
    "application_submitted": "application_id",
    "application_decided": "application_id",
    "report_signed": "month",
    "enforcement_recorded": "case_id",
}

_HOUR_FIELDS = {
    "from_hour", "to_hour", "start_hour", "end_hour", "valid_from",
    "effective_from", "event_hour",
}
_MONTH_FIELDS = {"observed_month", "month"}


@dataclass(frozen=True, slots=True)
class Event:
    seq: int | None
    event_type: str
    payload: dict[str, Any]
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "payload": self.payload,
            "fingerprint": self.fingerprint,
        }


def identity_key(event_type: str, payload: dict[str, Any]) -> str:
    """业务幂等键：同一原始记录/同一业务单据只能入账一次。"""
    field = IDEMPOTENCY_FIELD[event_type]
    if event_type == "permit_revised":
        return f"{event_type}:{payload['permit_id']}:{payload[field]}"
    if event_type in {"permit_suspended", "permit_resumed"}:
        return f"{event_type}:{payload['permit_id']}:{payload[field]}"
    return f"{event_type}:{payload[field]}"


def _validate(event_type: str, payload: dict[str, Any]) -> None:
    if event_type not in FIELDS:
        raise ValueError(f"未知事件类型 {event_type}")
    spec = FIELDS[event_type]
    allowed = {name for name, _ in spec}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{event_type} 存在未知字段: {sorted(unknown)}")
    for name, nullable in spec:
        value = payload.get(name)
        if value is None:
            if not nullable:
                raise ValueError(f"{event_type}.{name} 不能为空")
            continue
        if name in _HOUR_FIELDS:
            parse_hour(str(value))
        elif name in _MONTH_FIELDS:
            month = str(value)
            if len(month) != 7:
                raise ValueError(f"会计月份格式应为 YYYY-MM: {month!r}")
    if event_type in {"measurement_recorded", "inflow_recorded", "return_recorded"}:
        month_of(str(payload["event_hour"]))  # 语法校验
    if (
        event_type in {"measurement_amended", "return_amended", "inflow_amended"}
        and payload["kind"] not in {"correction", "backfill"}
    ):
        raise ValueError(f"{event_type}.kind 必须是 correction 或 backfill")
    if event_type == "application_decided" and payload["decision"] not in {
        "approved", "rejected",
    }:
        raise ValueError("decision 必须是 approved 或 rejected")
    if event_type == "enforcement_recorded" and payload["status"] not in {
        "open", "confirmed", "no_finding", "appeal_upheld", "appeal_rejected",
    }:
        raise ValueError("执法状态不合法")


def make_event(event_type: str, payload: dict[str, Any], seq: int | None = None) -> Event:
    """构造并校验事件，指纹只取决于类型与负载（与入账顺序无关）。"""
    _validate(event_type, payload)
    canon = json.dumps(
        {"event_type": event_type, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return Event(
        seq=seq,
        event_type=event_type,
        payload=dict(payload),
        fingerprint=sha256(canon.encode("utf-8")).hexdigest(),
    )
