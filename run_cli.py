"""生态用水履约核算命令行冒烟入口：构造一个最小可运行场景。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from water_compliance import ComplianceService, Ledger  # noqa: E402


def main() -> None:
    svc = ComplianceService(Ledger(":memory:"))
    svc.register_permit(
        permit_id="P-DEMO-01", point="PT-01", subject_id="S-DEMO",
        subject_name="示范取水户", use_codes=["industrial"],
        priority_order=3, hourly_limit_m3=100.0,
        effective_from="2026-10-01T00", doc_ref="DEMO-PERMIT")
    svc.open_quota(
        quota_id="Q-DEMO-01", permit_id="P-DEMO-01", use_code="industrial",
        season_label="枯水期", start_hour="2026-10-01T00",
        end_hour="2027-04-01T00", quota_m3=1000.0, doc_ref="DEMO-QUOTA")
    svc.record_measurement(
        raw_id="RAW-DEMO-01", permit_id="P-DEMO-01",
        event_hour="2026-10-05T14", use_code="industrial",
        gross_m3=80.0, estimate=False, source="在线流量计")
    available = svc.availability("P-DEMO-01", "industrial", "2026-10-05T14")
    print(json.dumps(available, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
