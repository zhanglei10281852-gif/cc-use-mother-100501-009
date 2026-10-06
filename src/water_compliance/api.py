"""HTTP API：仅依赖标准库，线程安全地包装 ComplianceService。

数据目录由环境变量 ``WATER_COMPLIANCE_HOME`` 指定（含 events.jsonl 哈希链与
documents.jsonl 快照库）；未设置时使用内存库，便于测试。

所有写接口都是幂等的：以业务记录标识作为幂等键，重复提交同一原始记录
返回首次结果而不会多扣额度。
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .ledger import EventChainBroken, IdempotencyConflict
from .reporting import ReportAlreadySigned, ReportNotFound
from .services import ComplianceService, ServiceError
from .ledger import EventStore
from .reporting import DocumentStore


def build_service(home: str | Path | None = None) -> ComplianceService:
    if home is None:
        home = os.environ.get("WATER_COMPLIANCE_HOME")
    if home:
        path = Path(home)
        path.mkdir(parents=True, exist_ok=True)
        store = EventStore(path / "events.jsonl")
        documents = DocumentStore(path / "documents.jsonl")
    else:
        store = EventStore()
        documents = DocumentStore()
    return ComplianceService(store, documents)


class _Handler(BaseHTTPRequestHandler):
    service: ComplianceService  # 由 make_server 注入到类上
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ---- 响应工具 ----

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        if hasattr(self, "_cached_body"):
            return self._cached_body  # type: ignore[has-type]
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            self._cached_body: dict[str, Any] = {}
            return self._cached_body
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        self._cached_body = payload
        return payload

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            handler = self._match(method, path, query)
            if handler is None:
                self._send(404, {"error": f"无此路由: {method} {path}"})
                return
            handler()
        except IdempotencyConflict as exc:
            self._send(409, {"error": str(exc)})
        except ReportAlreadySigned as exc:
            self._send(409, {"error": str(exc)})
        except (ServiceError, ReportNotFound) as exc:
            self._send(400, {"error": str(exc)})
        except KeyError as exc:
            self._send(404, {"error": str(exc).strip("'")})
        except (EventChainBroken,) as exc:
            self._send(500, {"error": f"账本完整性校验失败: {exc}"})

    def _match(self, method: str, path: str, query: dict[str, str]) -> Callable[[], None] | None:
        svc = self.service
        body = self._body if method == "POST" else lambda: {}
        routes: list[tuple[str, str, Callable[[], Any]]] = []

        if method == "GET":
            routes += [
                ("GET", "/health", lambda: {"status": "ok", "events": svc.store.version()}),
                ("GET", "/events", lambda: {
                    "events": [
                        {
                            "seq": item.seq,
                            "event_id": item.event_id,
                            "type": type(item.event).__name__,
                            "idempotency_key": item.idempotency_key,
                            "hash": item.hash,
                            "body": {
                                name: getattr(item.event, name)
                                for name in getattr(item.event, "__slots__", ())
                            },
                        }
                        for item in svc.store.events()
                    ]
                }),
                ("GET", "/points", lambda: {
                    "points": [
                        {
                            "point_id": p.point_id,
                            "name": p.name,
                            "user_id": p.user_id,
                            "control_section_id": p.control_section_id,
                        }
                        for p in svc.projection.points.values()
                    ]
                }),
                ("GET", "/sections", lambda: {
                    "sections": [
                        {"section_id": s.section_id, "name": s.name}
                        for s in svc.projection.sections.values()
                    ]
                }),
            ]
            m = re.fullmatch(r"/points/([^/]+)/recompute", path)
            if m:
                return lambda: self._send(
                    200,
                    svc.recompute(m.group(1), query["start"], query["end"]),
                )
            m = re.fullmatch(r"/points/([^/]+)/alerts", path)
            if m:
                return lambda: self._send(
                    200,
                    svc.alerts(m.group(1), query["start"], query["end"]),
                )
            m = re.fullmatch(r"/points/([^/]+)/reports/(\d{4}-\d{2})", path)
            if m:
                year, month = (int(part) for part in m.group(2).split("-"))
                return lambda: self._send(
                    200, svc.signed_report_body(m.group(1), year, month)
                )
            m = re.fullmatch(r"/points/([^/]+)/reports/(\d{4}-\d{2})/difference", path)
            if m:
                year, month = (int(part) for part in m.group(2).split("-"))
                return lambda: self._send(
                    200, svc.monthly_difference(m.group(1), year, month)
                )
            m = re.fullmatch(r"/sections/([^/]+)/recompute", path)
            if m:
                return lambda: self._send(
                    200,
                    svc.section_recompute(m.group(1), query["start"], query["end"]),
                )
            m = re.fullmatch(r"/requests/([^/]+)", path)
            if m:
                requests = {r.request_id: r for r in svc.projection.temporary_requests()}
                request = requests.get(m.group(1))
                if request is None:
                    raise KeyError(f"申请不存在: {m.group(1)}")
                return lambda: self._send(200, {
                    "request_id": request.request_id,
                    "point_id": request.point_id,
                    "start": request.interval.start_text(),
                    "end": request.interval.end_text(),
                    "volume_m3": request.volume_m3,
                    "decision": request.decision,
                    "impact_assessment_ref": request.impact_assessment_ref,
                })

        if method == "POST":
            m = re.fullmatch(r"/users", path)
            if m:
                return lambda: self._stored(
                    svc.register_user(**_pick(body(), ("user_id", "name", "kind")))
                )
            if path == "/sections":
                return lambda: self._stored(
                    svc.register_section(
                        body()["section_id"],
                        body()["name"],
                        body().get("environmental_rules", []),
                    )
                )
            if path == "/points":
                data = body()
                return lambda: self._stored(
                    svc.register_point(
                        point_id=data["point_id"],
                        name=data["name"],
                        user_id=data["user_id"],
                        river_reach=data["river_reach"],
                        control_section_id=data["control_section_id"],
                        routing_lag_hours=data.get("routing_lag_hours", 0),
                        routing_factor=data.get("routing_factor", 1.0),
                    )
                )
            m = re.fullmatch(r"/points/([^/]+)/permit-versions", path)
            if m:
                data = body()
                return lambda: self._stored(
                    svc.add_permit_version(
                        point_id=m.group(1),
                        revision=data["revision"],
                        valid_from=data["valid_from"],
                        valid_to=data.get("valid_to"),
                        annual_quota_m3=data["annual_quota_m3"],
                        purpose_codes=data["purpose_codes"],
                        priority_subjects=data.get("priority_subjects", []),
                        seasonal_quotas=data.get("seasonal_quotas", []),
                        dry_season_months=data.get("dry_season_months", []),
                        note=data.get("note", ""),
                    )
                )
            m = re.fullmatch(r"/points/([^/]+)/suspensions", path)
            if m:
                data = body()
                return lambda: self._stored(
                    svc.suspend_permit(
                        point_id=m.group(1),
                        revision=data["revision"],
                        suspend_from=data["suspend_from"],
                        suspend_to=data.get("suspend_to"),
                        reason=data["reason"],
                        enforcement_ref=data.get("enforcement_ref", ""),
                    )
                )
            if path == "/resumptions":
                data = body()
                return lambda: self._stored(
                    svc.resume_permit(
                        data["point_id"],
                        data["suspend_from"],
                        data["resume_at"],
                        data.get("reason", ""),
                    )
                )
            if path == "/meter-records":
                data = body()
                return lambda: self._stored(
                    svc.record_meter(
                        record_id=data["record_id"],
                        point_id=data["point_id"],
                        hour=data["hour"],
                        withdrawal_m3=data["withdrawal_m3"],
                        source=data.get("source", "measured"),
                        estimate_source=data.get("estimate_source", ""),
                        note=data.get("note", ""),
                    )
                )
            if path == "/return-records":
                data = body()
                return lambda: self._stored(
                    svc.record_return(
                        record_id=data["record_id"],
                        point_id=data["point_id"],
                        hour=data["hour"],
                        return_m3=data["return_m3"],
                        to_section_id=data["to_section_id"],
                        source=data.get("source", "measured"),
                        estimate_source=data.get("estimate_source", ""),
                    )
                )
            if path == "/meter-amendments":
                data = body()
                return lambda: self._stored(
                    svc.amend_meter(
                        amendment_id=data["amendment_id"],
                        point_id=data["point_id"],
                        hour=data["hour"],
                        corrected_withdrawal_m3=data["corrected_withdrawal_m3"],
                        replaces_record=data["replaces_record"],
                        reason=data["reason"],
                        source=data.get("source", "measured"),
                    )
                )
            if path == "/return-amendments":
                data = body()
                return lambda: self._stored(
                    svc.amend_return(
                        amendment_id=data["amendment_id"],
                        point_id=data["point_id"],
                        hour=data["hour"],
                        corrected_return_m3=data["corrected_return_m3"],
                        replaces_record=data["replaces_record"],
                        reason=data["reason"],
                        source=data.get("source", "measured"),
                    )
                )
            if path == "/section-inflows":
                data = body()
                return lambda: self._stored(
                    svc.record_section_inflow(
                        record_id=data["record_id"],
                        section_id=data["section_id"],
                        hour=data["hour"],
                        inflow_m3=data["inflow_m3"],
                        source=data.get("source", "measured"),
                        estimate_source=data.get("estimate_source", ""),
                    )
                )
            if path == "/section-inflow-amendments":
                data = body()
                return lambda: self._stored(
                    svc.amend_section_inflow(
                        amendment_id=data["amendment_id"],
                        section_id=data["section_id"],
                        hour=data["hour"],
                        corrected_inflow_m3=data["corrected_inflow_m3"],
                        replaces_record=data["replaces_record"],
                        reason=data["reason"],
                        source=data.get("source", "measured"),
                    )
                )
            if path == "/transfers":
                data = body()
                return lambda: self._stored(
                    svc.approve_transfer(
                        transfer_id=data["transfer_id"],
                        from_point_id=data["from_point_id"],
                        to_point_id=data["to_point_id"],
                        valid_from=data["valid_from"],
                        valid_to=data["valid_to"],
                        volume_m3=data["volume_m3"],
                        approval_ref=data["approval_ref"],
                        purpose_code=data.get("purpose_code", "transfer"),
                    )
                )
            if path == "/transfer-revocations":
                data = body()
                return lambda: self._stored(
                    svc.revoke_transfer(
                        data["transfer_id"], data["revoked_at"], data["reason"]
                    )
                )
            if path == "/impact-assessments":
                data = body()
                return lambda: self._send(
                    200,
                    svc.assess_impact(
                        data["point_id"],
                        data["valid_from"],
                        data["valid_to"],
                        data["volume_m3"],
                    ),
                )
            if path == "/requests":
                data = body()
                return lambda: self._stored(
                    svc.submit_request(
                        request_id=data["request_id"],
                        point_id=data["point_id"],
                        valid_from=data["valid_from"],
                        valid_to=data["valid_to"],
                        volume_m3=data["volume_m3"],
                        purpose_code=data["purpose_code"],
                        applicant=data["applicant"],
                        impact_assessment_ref=data["impact_assessment_ref"],
                    )
                )
            m = re.fullmatch(r"/requests/([^/]+)/decision", path)
            if m:
                data = body()
                return lambda: self._stored(
                    svc.decide_request(
                        request_id=m.group(1),
                        decision=data["decision"],
                        decided_by=data["decided_by"],
                        reason=data.get("reason", ""),
                        override=data.get("override", False),
                    )
                )
            m = re.fullmatch(r"/points/([^/]+)/reports", path)
            if m:
                data = body()
                return lambda: self._send(
                    201,
                    svc.sign_monthly_report(
                        point_id=m.group(1),
                        year=int(data["year"]),
                        month=int(data["month"]),
                        signed_by=data["signed_by"],
                    ),
                )
            if path == "/reviews":
                data = body()
                return lambda: self._stored(
                    svc.log_review(
                        review_id=data["review_id"],
                        point_id=data["point_id"],
                        valid_from=data["valid_from"],
                        valid_to=data["valid_to"],
                        conclusion=data["conclusion"],
                        reviewer=data["reviewer"],
                        note=data.get("note", ""),
                    )
                )
            if path == "/appeals":
                data = body()
                return lambda: self._stored(
                    svc.log_appeal(
                        appeal_id=data["appeal_id"],
                        review_id=data["review_id"],
                        decision=data["decision"],
                        decided_by=data["decided_by"],
                        note=data.get("note", ""),
                    )
                )
            if path == "/evidence":
                data = body()
                return lambda: self._send(200, {"evidence": svc.evidence(data["record_ids"])})

        for route_method, route_path, handler in routes:
            if route_method == method and route_path == path:
                return lambda: self._send(200, handler())
        return None

    def _stored(self, stored: Any) -> None:
        self._send(
            201,
            {
                "seq": stored.seq,
                "event_id": stored.event_id,
                "type": type(stored.event).__name__,
                "idempotent": stored.idempotency_key is not None,
            },
        )


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: data[key] for key in keys}


def make_server(host: str, port: int, home: str | Path | None = None) -> ThreadingHTTPServer:
    service = build_service(home)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8080, home: str | Path | None = None) -> None:
    server = make_server(host, port, home)
    actual_host, actual_port = server.server_address[:2]
    print(f"生态用水履约核算服务已启动: http://{actual_host}:{actual_port}")
    if home:
        print(f"数据目录: {Path(home).resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
