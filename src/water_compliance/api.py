"""基于标准库 ``http.server`` 的 JSON API。

路由层不包含业务规则，只负责把 JSON 请求交给 :class:`ComplianceService`。
所有结论接口都返回证据事件的 seq，可用 ``/events/{seq}`` 逐条回溯。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from typing import Any, Callable

from .service import ComplianceService, ServiceError
from .storage import Ledger, LedgerError


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "EcoWaterCompliance/1.0"

    @property
    def service(self) -> ComplianceService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return body

    def _handle_service_error(self, exc: Exception) -> None:
        self._send(422, {"error": type(exc).__name__, "message": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except ServiceError as exc:
            self._handle_service_error(exc)
        except LedgerError as exc:
            self._handle_service_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except ServiceError as exc:
            self._handle_service_error(exc)
        except LedgerError as exc:
            self._handle_service_error(exc)

    # ------------------------------------------------------------------ GET
    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if parts == ["events"]:
            self._send(200, [e.to_dict() for e in self.service.ledger.all_events()])
            return
        if len(parts) == 2 and parts[0] == "events":
            event = self.service.event(int(parts[1]))
            if event is None:
                self._send(404, {"error": "NotFound", "message": "事件不存在"})
            else:
                self._send(200, event)
            return
        if parts == ["availability"]:
            self._send(200, self.service.availability(
                query["permit_id"], query["use_code"], query["hour"]))
            return
        if parts == ["compliance"]:
            self._send(200, self.service.compliance(query["start"], query["end"]))
            return
        if parts == ["alerts"]:
            self._send(200, self.service.alert_basis(query["start"], query["end"]))
            return
        if len(parts) == 2 and parts[0] == "reports":
            proj = self.service.projection()
            report = proj.reports.get(parts[1])
            if report is None:
                self._send(404, {"error": "NotFound", "message": "月报不存在"})
            else:
                self._send(200, report)
            return
        if len(parts) == 3 and parts[0] == "reports" and parts[2] == "verify":
            self._send(200, self.service.verify_report(parts[1]))
            return
        if parts == ["health"]:
            self._send(200, {"status": "ok",
                             "events": self.service.ledger.latest_seq()})
            return
        self._send(404, {"error": "NotFound", "message": f"未知路径: {self.path}"})

    # ----------------------------------------------------------------- POST
    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        body = self._read_body()

        routes: dict[tuple[str, ...], Callable[[], None]] = {
            ("permits",): lambda: self._send(200, self.service.register_permit(**body).__dict__),
            ("permits", "revise"): lambda: self._send(200, self.service.revise_permit(**body).__dict__),
            ("permits", "suspend"): lambda: self._send(200, self.service.suspend_permit(**body).__dict__),
            ("permits", "resume"): lambda: self._send(200, self.service.resume_permit(**body).__dict__),
            ("quotas",): lambda: self._send(200, self.service.open_quota(**body).__dict__),
            ("quotas", "amend"): lambda: self._send(200, self.service.amend_quota(**body).__dict__),
            ("sections",): lambda: self._send(200, self.service.register_section(**body).__dict__),
            ("sections", "schedule"): lambda: self._send(200, self.service.schedule_section_requirement(**body).__dict__),
            ("sections", "link"): lambda: self._send(200, self.service.link_section_point(**body).__dict__),
            ("measurements",): lambda: self._send(200, self.service.record_measurement(**body).__dict__),
            ("measurements", "amend"): lambda: self._send(200, self.service.amend_measurement(**body).__dict__),
            ("returns",): lambda: self._send(200, self.service.record_return(**body).__dict__),
            ("returns", "amend"): lambda: self._send(200, self.service.amend_return(**body).__dict__),
            ("inflows",): lambda: self._send(200, self.service.record_inflow(**body).__dict__),
            ("inflows", "amend"): lambda: self._send(200, self.service.amend_inflow(**body).__dict__),
            ("transfers",): lambda: self._send(200, self.service.approve_transfer(**body).__dict__),
            ("enforcement",): lambda: self._send(200, self.service.record_enforcement(**body).__dict__),
        }
        key = tuple(parts)
        if key == ("applications", "evaluate"):
            self._send(200, self.service.evaluate_application(**body))
            return
        if key == ("applications",):
            self._send(200, self.service.submit_application(**body))
            return
        if key == ("applications", "decide"):
            self._send(200, self.service.decide_application(**body).__dict__)
            return
        if key == ("reports", "sign"):
            self._send(200, self.service.sign_report(**body).__dict__)
            return
        if key in routes:
            routes[key]()
            return
        self._send(404, {"error": "NotFound", "message": f"未知路径: {self.path}"})


def create_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = ComplianceService(Ledger(db_path))  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "water.db") -> None:
    httpd = create_server(host, port, db_path)
    print(f"生态用水履约核算服务监听 http://{host}:{port} 台账={db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
