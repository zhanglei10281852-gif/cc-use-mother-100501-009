"""HTTP API 端到端测试：在真实端口上走完整业务链路。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
import urllib.parse

from water_compliance.api import make_server


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = make_server("127.0.0.1", 0, home=None)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return _request("POST", self.base + path, payload)

    def get(self, path: str) -> tuple[int, dict]:
        return _request("GET", self.base + path)

    def _seed(self) -> None:
        self.post("/users", {"user_id": "U1", "name": "工业园", "kind": "industrial_park"})
        self.post("/sections", {
            "section_id": "S1", "name": "断面",
            "environmental_rules": [["dry", "11-01", "03-31", 500.0]],
        })
        self.post("/points", {
            "point_id": "P1", "name": "取水口", "user_id": "U1",
            "river_reach": "东河", "control_section_id": "S1",
        })
        self.post("/points/P1/permit-versions", {
            "revision": "V1", "valid_from": "2026-01-01T00:00+08:00",
            "annual_quota_m3": 36_000.0, "purpose_codes": ["production"],
            "priority_subjects": ["工业"],
            "seasonal_quotas": [["dry", "11-01", "11-30", 36_000.0]],
        })

    def test_full_flow_including_duplicate_import(self) -> None:
        self._seed()

        # 重复导入同一原始记录
        meter = {
            "record_id": "m1", "point_id": "P1",
            "hour": "2026-11-01T00:00+08:00", "withdrawal_m3": 70.0,
        }
        status1, body1 = self.post("/meter-records", meter)
        status2, body2 = self.post("/meter-records", meter)
        self.assertEqual(status1, 201)
        self.assertEqual(body1["seq"], body2["seq"])

        status, recompute = self.get(
            "/points/P1/recompute?"
            + urllib.parse.urlencode(
                {"start": "2026-11-01T00:00+08:00", "end": "2026-11-01T01:00+08:00"}
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(recompute["total_withdrawal_m3"], 70.0)

        # 暂估缺测必须带来源
        status, body = self.post("/meter-records", {
            "record_id": "e1", "point_id": "P1",
            "hour": "2026-11-01T01:00+08:00", "withdrawal_m3": 50.0,
            "source": "estimated",
        })
        self.assertEqual(status, 400)

        # 告警接口
        status, alerts = self.get(
            "/points/P1/alerts?"
            + urllib.parse.urlencode(
                {"start": "2026-11-01T00:00+08:00", "end": "2026-11-01T02:00+08:00"}
            )
        )
        self.assertEqual(status, 200)
        self.assertIn("alerts", alerts)

        # 证据回溯
        status, evidence = self.post("/evidence", {"record_ids": ["m1"]})
        self.assertEqual(status, 200)
        self.assertEqual(evidence["evidence"][0]["event_type"], "HourlyMeterRecorded")

    def test_report_sign_then_amendment_diff(self) -> None:
        self._seed()
        self.post("/meter-records", {
            "record_id": "r1", "point_id": "P1",
            "hour": "2026-11-01T00:00+08:00", "withdrawal_m3": 40.0,
        })
        status, signed = self.post("/points/P1/reports", {
            "year": 2026, "month": 11, "signed_by": "王",
        })
        self.assertEqual(status, 201)

        # 重签 409
        status, _ = self.post("/points/P1/reports", {
            "year": 2026, "month": 11, "signed_by": "王",
        })
        self.assertEqual(status, 409)

        # 补报后差异调整
        self.post("/meter-amendments", {
            "amendment_id": "a1", "point_id": "P1",
            "hour": "2026-11-01T00:00+08:00", "corrected_withdrawal_m3": 55.0,
            "replaces_record": "r1", "reason": "补传",
        })
        status, diff = self.get("/points/P1/reports/2026-11/difference")
        self.assertEqual(status, 200)
        self.assertEqual(diff["changed_hours"][0]["delta_withdrawal_m3"], 15.0)

        # 已签署正文保持 40
        status, body = self.get("/points/P1/reports/2026-11")
        self.assertEqual(
            body["summary"]["total_withdrawal_m3"], 40.0
        )

    def test_request_impact_then_rejection(self) -> None:
        self._seed()
        self.post("/section-inflows", {
            "record_id": "i1", "section_id": "S1",
            "hour": "2026-11-01T00:00+08:00", "inflow_m3": 560.0,
        })
        # 560 - 500 余量仅 60；申请 100/h 不可行
        status, assessment = self.post("/impact-assessments", {
            "point_id": "P1",
            "valid_from": "2026-11-01T00:00+08:00",
            "valid_to": "2026-11-01T01:00+08:00",
            "volume_m3": 100.0,
        })
        self.assertEqual(status, 200)
        self.assertFalse(assessment["feasible"])

        status, _ = self.post("/requests", {
            "request_id": "Q1", "point_id": "P1",
            "valid_from": "2026-11-01T00:00+08:00",
            "valid_to": "2026-11-01T01:00+08:00",
            "volume_m3": 100.0, "purpose_code": "production",
            "applicant": "工业园",
            "impact_assessment_ref": assessment["assessment_ref"],
        })
        self.assertEqual(status, 201)
        status, body = self.post("/requests/Q1/decision", {
            "decision": "approved", "decided_by": "李", "reason": "试批",
        })
        self.assertEqual(status, 400)
        self.assertIn("生态基流", body["error"])


if __name__ == "__main__":
    unittest.main()
