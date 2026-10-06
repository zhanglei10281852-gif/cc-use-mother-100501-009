"""月报签署与差异调整。

签署时生成报告正文快照（内容寻址、不可变），事件只保存其指纹；
签署后到达的计量更正、许可暂停、调剂等一律不得改写已签署正文，
只能由 :func:`difference_adjustment` 对照"签署快照 vs 当前重算"生成差异调整。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import canonical_hash
from .engine import recompute_point
from .projection import Projection
from .timeutil import month_range


class ReportAlreadySigned(ValueError):
    """同一取水点同一月份只能签署一次。"""


class ReportNotFound(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class StoredDocument:
    doc_id: str
    body: dict[str, Any]


class DocumentStore:
    """内容寻址的不可变文档库（JSONL，每行一个快照）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._docs: dict[str, dict[str, Any]] = {}
        if self._path and self._path.exists():
            import json as _json

            for raw in self._path.read_text(encoding="utf-8").splitlines():
                if raw.strip():
                    row = _json.loads(raw)
                    self._docs[row["doc_id"]] = row["body"]

    def put(self, body: dict[str, Any]) -> str:
        doc_id = canonical_hash(body)
        if doc_id not in self._docs:
            self._docs[doc_id] = body
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"doc_id": doc_id, "body": body}, ensure_ascii=False))
                    handle.write("\n")
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any]:
        if doc_id not in self._docs:
            raise KeyError(f"文档不存在: {doc_id}")
        return self._docs[doc_id]

    def get_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        """按内容摘要前缀（如 impact- 编号携带的 16 位）检索文档。"""
        matches = [doc_id for doc_id in self._docs if doc_id.startswith(prefix.removeprefix("impact-"))]
        if not matches:
            return None
        if len(matches) > 1:
            raise KeyError(f"文档前缀 {prefix!r} 命中多条记录")
        return self._docs[matches[0]]

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._docs


def build_monthly_body(
    projection: Projection, point_id: str, year: int, month: int
) -> dict[str, Any]:
    """构造月报正文（签署前的可复算内容）。"""
    period = month_range(year, month)
    result = recompute_point(projection, point_id, period)
    return {
        "kind": "monthly_water_report",
        "version": 1,
        "point_id": point_id,
        "period": f"{year:04d}-{month:02d}",
        "start": period.start_text(),
        "end": period.end_text(),
        "summary": {
            "total_quota_m3": round(result.total_quota_m3, 3),
            "total_withdrawal_m3": round(result.total_withdrawal_m3, 3),
            "total_return_m3": round(result.total_return_m3, 3),
            "total_estimated_m3": round(result.total_estimated_m3, 3),
            "balance_m3": round(result.total_quota_m3 - result.total_withdrawal_m3, 3),
        },
        "status_intervals": result.as_dict()["status_intervals"],
        "hours": result.as_dict()["hours"],
        "basis": list(result.basis),
    }


def difference_adjustment(
    projection: Projection,
    documents: DocumentStore,
    point_id: str,
    year: int,
    month: int,
) -> dict[str, Any]:
    """已签署快照与当前重算之间的差异调整（不修改签署正文）。"""
    period = f"{year:04d}-{month:02d}"
    signed = projection.reports.get((point_id, period))
    if signed is None:
        raise ReportNotFound(f"{point_id} {period} 尚未签署月报")
    frozen = documents.get(signed.body_fingerprint)
    current = build_monthly_body(projection, point_id, year, month)

    frozen_hours = {row["hour"]: row for row in frozen["hours"]}
    current_hours = {row["hour"]: row for row in current["hours"]}
    # 只比较逐时原始口径；累计余额/状态是派生值，其变化由汇总与差异总量体现
    primary_fields = (
        "permit_revision",
        "suspended",
        "base_quota_m3",
        "temp_quota_m3",
        "transfer_in_m3",
        "transfer_out_m3",
        "total_quota_m3",
        "withdrawal_m3",
        "return_m3",
        "estimated",
    )
    changed: list[dict[str, Any]] = []
    for hour in sorted(set(frozen_hours) | set(current_hours)):
        before = frozen_hours.get(hour)
        after = current_hours.get(hour)
        before_cmp = (
            {field: before.get(field) for field in primary_fields} if before else None
        )
        after_cmp = (
            {field: after.get(field) for field in primary_fields} if after else None
        )
        if before_cmp == after_cmp:
            continue
        changed.append(
            {
                "hour": hour,
                "signed": before,
                "recomputed": after,
                "delta_withdrawal_m3": round(
                    (after["withdrawal_m3"] if after else 0.0)
                    - (before["withdrawal_m3"] if before else 0.0),
                    3,
                ),
                "delta_quota_m3": round(
                    (after["total_quota_m3"] if after else 0.0)
                    - (before["total_quota_m3"] if before else 0.0),
                    3,
                ),
            }
        )

    return {
        "kind": "monthly_difference_adjustment",
        "report_id": signed.report_id,
        "point_id": point_id,
        "period": period,
        "signed_fingerprint": signed.body_fingerprint,
        "signed_at": signed.signed_at,
        "signed_summary": frozen["summary"],
        "current_summary": current["summary"],
        "changed_hours": changed,
        "note": "差异仅作调整，不重写已签署月报；调整依据见各小时 basis 记录。",
    }
