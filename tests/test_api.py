"""HTTP API 端到端集成测试。"""

from __future__ import annotations

import json
import threading
import unittest
from urllib import request as urlrequest
from urllib.error import HTTPError

from water_compliance.api import create_server


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0, ":memory:")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.service.ledger.close()

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_workflow_via_api(self) -> None:
        status, permit = self._call("POST", "/permits", {
            "permit_id": "P-1", "point": "PT-A", "subject_id": "S-1",
            "subject_name": "工业园", "use_codes": ["industrial"],
            "priority_order": 3, "hourly_limit_m3": 100.0,
            "effective_from": "2026-10-01T00", "doc_ref": "证-1"})
        self.assertEqual(status, 200)

        # 重复导入同一条原始记录：第二次 duplicate=true，同一 seq
        payload = {"raw_id": "R-1", "permit_id": "P-1",
                   "event_hour": "2026-10-05T10", "use_code": "industrial",
                   "gross_m3": 60.0, "source": "在线表",
                   "observed_month": "2026-10"}
        _, first = self._call("POST", "/measurements", payload)
        _, second = self._call("POST", "/measurements", payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["seq"], second["seq"])

        status, avail = self._call(
            "GET", "/availability?permit_id=P-1&use_code=industrial&hour=2026-10-05T10")
        self.assertEqual(status, 200)
        self.assertEqual(avail["used_this_hour_m3"], 60.0)

        # 申请超小时限值应被拒绝（11 点尚无既有取水，120 > 100）
        status, assessment = self._call("POST", "/applications/evaluate", {
            "permit_id": "P-1", "use_code": "industrial",
            "start_hour": "2026-10-05T11", "end_hour": "2026-10-05T12",
            "requested_m3_per_hour": 120.0})
        self.assertEqual(status, 200)
        self.assertEqual(assessment["decision"], "rejected")
        self.assertTrue(any(v["rule"] == "hourly_limit_exceeded"
                            for v in assessment["violations"]))

        # 同号不同内容 → 422
        bad = dict(payload, gross_m3=99.0)
        status, error = self._call("POST", "/measurements", bad)
        self.assertEqual(status, 422)
        self.assertEqual(error["error"], "ServiceError")

        status, alerts = self._call(
            "GET", "/alerts?start=2026-10-05T00&end=2026-10-06T00")
        self.assertEqual(status, 200)
        self.assertIn("P-1", alerts["permits"])

        status, health = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")

    def test_unknown_route_404(self) -> None:
        status, body = self._call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
