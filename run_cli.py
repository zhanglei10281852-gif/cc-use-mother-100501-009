"""生态用水履约核算命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from water_compliance import PermitLedger


def main() -> None:
    item = PermitLedger(ledger_code='ledger-code-001', withdrawal_point='withdrawal-point-001', permit_revision='permit-revision-001', state='state-001')
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
