"""只追加（append-only）领域事件。

账本的全部状态都由这些事件重放得到；事件一经接受即不可修改、不可删除。
更正类业务（计量补报、许可暂停、调剂撤销）不修改旧事件，而是追加新的
调整事件，由核算引擎将差异落到新签署的报告或差异调整记录中。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

# ---- 主体与取水点 ----


@dataclass(frozen=True, slots=True)
class RegisteredWaterUser:
    user_id: str
    name: str
    kind: str  # industrial_park | irrigation_district | urban_supply | other


@dataclass(frozen=True, slots=True)
class RegisteredWithdrawalPoint:
    point_id: str
    name: str
    user_id: str
    river_reach: str
    # 该点取水汇入的生态控制断面（取水量经汇流系数影响断面水量）
    control_section_id: str
    routing_lag_hours: int = 0
    routing_factor: float = 1.0


# ---- 许可版本（不可变，新版本追加生效）----


@dataclass(frozen=True, slots=True)
class PermitVersionAdded:
    point_id: str
    revision: str
    valid_from: str  # 整点
    valid_to: str | None  # None 表示长期有效，被下一版本截断
    annual_quota_m3: float
    purpose_codes: tuple[str, ...]  # 允许用途
    priority_subjects: tuple[str, ...]  # 优先保障对象
    seasonal_quotas: tuple[tuple[str, str, str, float], ...]
    # 每项: (季节编码, 起始月日 MM-DD, 结束月日 MM-DD, 季节总额 m³)
    dry_season_months: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class PermitSuspensionLogged:
    """许可暂停/恢复；暂停区间内可用额度为零。恢复也是追加事件，不改写。"""

    point_id: str
    revision: str
    suspend_from: str
    suspend_to: str | None  # None 表示暂停至另行通知；恢复事件结束它
    reason: str
    enforcement_ref: str = ""


@dataclass(frozen=True, slots=True)
class PermitResumptionLogged:
    point_id: str
    suspend_from: str  # 与被恢复的暂停事件起点对应
    resume_at: str
    reason: str


# ---- 生态控制断面 ----


@dataclass(frozen=True, slots=True)
class ControlSectionRegistered:
    section_id: str
    name: str
    # 生态基流要求按季节逐时维护（替代"生态下泄单独口径"）：
    # (季节编码, MM-DD, MM-DD, 每小时最低下泄 m³/h)
    environmental_rules: tuple[tuple[str, str, str, float], ...]


@dataclass(frozen=True, slots=True)
class SectionInflowRecorded:
    """控制断面逐时实测天然来水（断面口径计量，同样支持暂估与更正）。"""

    record_id: str
    section_id: str
    hour: str
    inflow_m3: float
    source: str = "measured"
    estimate_source: str = ""


@dataclass(frozen=True, slots=True)
class SectionInflowAmendmentRecorded:
    amendment_id: str
    section_id: str
    hour: str
    corrected_inflow_m3: float
    replaces_record: str
    reason: str
    source: str = "measured"


# ---- 逐时计量 ----


@dataclass(frozen=True, slots=True)
class HourlyMeterRecorded:
    """逐时取水量计量记录。

    source: measured（实测） | estimated（暂估，必须给出 estimate_source）
    同一记录以 record_id 幂等：重复导入返回既有结果，绝不二次扣减。
    """

    record_id: str
    point_id: str
    hour: str  # 整点小时桶起点
    withdrawal_m3: float
    source: str = "measured"
    estimate_source: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class HourlyReturnFlowRecorded:
    """逐时退水（回归河道/管网），在断面影响中按断面返还。"""

    record_id: str
    point_id: str
    hour: str
    return_m3: float
    to_section_id: str
    source: str = "measured"
    estimate_source: str = ""


@dataclass(frozen=True, slots=True)
class MeterAmendmentRecorded:
    """计量更正：以差异形式追加，不删除原记录。

    若原记录为暂估且补报为实测，``replaces_record`` 指向暂估记录；
    核算时原暂估记录被标记 superseded，净值只产生 补报-暂估 的差异。
    若月报已签署，则更正进入差异调整，不重写月报数字。
    """

    amendment_id: str
    point_id: str
    hour: str
    corrected_withdrawal_m3: float
    replaces_record: str  # 被更正的原始 record_id
    reason: str
    source: str = "measured"


@dataclass(frozen=True, slots=True)
class ReturnFlowAmendmentRecorded:
    amendment_id: str
    point_id: str
    hour: str
    corrected_return_m3: float
    replaces_record: str
    reason: str
    source: str = "measured"


# ---- 经批准的跨主体调剂 ----


@dataclass(frozen=True, slots=True)
class TransferApproved:
    """跨取水点（可跨主体）水量调剂，逐时落地。

    转出方在各小时承担 ``volume_m3`` 的额度占用（视为其取水），
    受让方获得等额额度；调剂本身不改变河道断面水量（仅在两点间转移指标）。
    已经用于履约的小时也可调剂（事后经批准），但其差异不得改写已签署月报。
    """

    transfer_id: str
    from_point_id: str
    to_point_id: str
    valid_from: str
    valid_to: str
    volume_m3: float
    approval_ref: str
    purpose_code: str = "transfer"
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class TransferRevocationLogged:
    transfer_id: str
    revoked_at: str
    reason: str


# ---- 临时增量申请（每次申请前做断面影响评估）----


@dataclass(frozen=True, slots=True)
class TemporaryRequestSubmitted:
    request_id: str
    point_id: str
    valid_from: str
    valid_to: str
    volume_m3: float  # 申请总量
    purpose_code: str
    applicant: str
    impact_assessment_ref: str  # 提交前计算的评估 id


@dataclass(frozen=True, slots=True)
class TemporaryRequestDecided:
    request_id: str
    decision: str  # approved | rejected
    decided_by: str
    reason: str
    # 批准后生成的专用额度版本（许可外追加额度），逐时生效
    approved_extra_quota_m3: float = 0.0


# ---- 月报（签署后冻结）----


@dataclass(frozen=True, slots=True)
class MonthlyReportSigned:
    report_id: str
    point_id: str
    period: str  # YYYY-MM
    body_fingerprint: str  # 报告正文摘要
    signed_by: str
    signed_at: str


# ---- 执法复核与申诉 ----


@dataclass(frozen=True, slots=True)
class EnforcementReviewLogged:
    review_id: str
    point_id: str
    valid_from: str
    valid_to: str
    conclusion: str  # confirmed_violation | waived | pending
    reviewer: str
    note: str


@dataclass(frozen=True, slots=True)
class AppealDecisionLogged:
    appeal_id: str
    review_id: str
    point_id: str
    decision: str  # upheld | overturned
    decided_by: str
    note: str


_EVENT_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        RegisteredWaterUser,
        RegisteredWithdrawalPoint,
        PermitVersionAdded,
        PermitSuspensionLogged,
        PermitResumptionLogged,
        ControlSectionRegistered,
        SectionInflowRecorded,
        SectionInflowAmendmentRecorded,
        HourlyMeterRecorded,
        HourlyReturnFlowRecorded,
        MeterAmendmentRecorded,
        ReturnFlowAmendmentRecorded,
        TransferApproved,
        TransferRevocationLogged,
        TemporaryRequestSubmitted,
        TemporaryRequestDecided,
        MonthlyReportSigned,
        EnforcementReviewLogged,
        AppealDecisionLogged,
    )
}


def event_type_name(event: Any) -> str:
    return type(event).__name__


def serialize_event(event: Any, seq: int, event_id: str) -> dict[str, Any]:
    body = {name: getattr(event, name) for name in getattr(event, "__slots__", ())}
    return {
        "event_id": event_id,
        "seq": seq,
        "type": event_type_name(event),
        "body": body,
    }


def deserialize_event(row: dict[str, Any]) -> Any:
    cls = _EVENT_TYPES.get(row["type"])
    if cls is None:
        raise ValueError(f"未知事件类型 {row['type']!r}")
    body = dict(row["body"])
    # tuple 字段还原
    for name, annotation in getattr(cls, "__annotations__", {}).items():
        if name in body and "tuple" in str(annotation) and isinstance(body[name], list):
            body[name] = tuple(tuple(item) if isinstance(item, list) else item for item in body[name])
    return cls(**body)


def canonical_hash(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(text.encode("utf-8")).hexdigest()
