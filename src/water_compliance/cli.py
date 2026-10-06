"""命令行工具：监管人员可从终端复算任一时段、查看告警与证据。

示例：
  python -m water_compliance.cli --home ./data serve --port 8080
  python -m water_compliance.cli --home ./data recompute P001 --start 2026-11-01T00:00+08:00 --end 2026-12-01T00:00+08:00
  python -m water_compliance.cli --home ./data alerts P001 --start ... --end ...
  python -m water_compliance.cli --home ./data evidence meter-001 permit:P001:V1
  python -m water_compliance.cli --home ./data import scenario.json
  python -m water_compliance.cli demo-init --home ./data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .api import build_service, serve
from .ledger import IdempotencyConflict
from .reporting import ReportAlreadySigned
from .services import ComplianceService, ServiceError


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build(args: argparse.Namespace) -> ComplianceService:
    return build_service(args.home)


def cmd_serve(args: argparse.Namespace) -> int:
    serve(args.host, args.port, args.home)
    return 0


def cmd_recompute(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.recompute(args.point_id, args.start, args.end))
    return 0


def cmd_section(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.section_recompute(args.section_id, args.start, args.end))
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.alerts(args.point_id, args.start, args.end))
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print({"evidence": svc.evidence(args.record_ids)})
    return 0


def cmd_report_sign(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.sign_monthly_report(args.point_id, args.year, args.month, args.signed_by))
    return 0


def cmd_report_show(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.signed_report_body(args.point_id, args.year, args.month))
    return 0


def cmd_report_diff(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(svc.monthly_difference(args.point_id, args.year, args.month))
    return 0


def cmd_assess(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(
        svc.assess_impact(args.point_id, args.start, args.end, args.volume_m3)
    )
    return 0


# ---- 批量导入（JSON 场景文件；全部命令幂等）----

_DISPATCH: list[tuple[str, str]] = [
    ("register_user", "register_user"),
    ("register_section", "register_section"),
    ("register_point", "register_point"),
    ("add_permit_version", "add_permit_version"),
    ("suspend_permit", "suspend_permit"),
    ("resume_permit", "resume_permit"),
    ("record_meter", "record_meter"),
    ("record_return", "record_return"),
    ("amend_meter", "amend_meter"),
    ("amend_return", "amend_return"),
    ("record_section_inflow", "record_section_inflow"),
    ("amend_section_inflow", "amend_section_inflow"),
    ("approve_transfer", "approve_transfer"),
    ("revoke_transfer", "revoke_transfer"),
    ("submit_request", "submit_request"),
    ("decide_request", "decide_request"),
    ("log_review", "log_review"),
    ("log_appeal", "log_appeal"),
]


def cmd_import(args: argparse.Namespace) -> int:
    svc = _build(args)
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    commands = payload["commands"] if isinstance(payload, dict) else payload
    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands, 1):
        op = command.get("op")
        method = dict(_DISPATCH).get(op)
        if method is None:
            raise SystemExit(f"第 {index} 条命令 op={op!r} 不支持")
        params = {key: value for key, value in command.items() if key != "op"}
        stored = getattr(svc, method)(**params)
        results.append(
            {"index": index, "op": op, "seq": stored.seq, "event_id": stored.event_id}
        )
    _print({"imported": len(results), "events": results})
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    svc = _build(args)
    _print(
        {
            "version": svc.store.version(),
            "events": [
                {"seq": item.seq, "type": type(item.event).__name__, "hash": item.hash}
                for item in svc.store.events()
            ],
        }
    )
    return 0


def cmd_demo_init(args: argparse.Namespace) -> int:
    """写入一个枯水期示例场景，便于冒烟与演示。"""
    from .demo import build_demo_commands

    if not args.home:
        raise SystemExit("demo-init 需要 --home 指定数据目录")
    path = Path(args.home)
    if path.exists() and any(path.glob("*.jsonl")) and not args.force:
        raise SystemExit(f"{path} 已有数据；如需追加请加 --force")
    svc = _build(args)
    n = 0
    for command in build_demo_commands():
        op = command["op"]
        params = {key: value for key, value in command.items() if key != "op"}
        getattr(svc, dict(_DISPATCH)[op])(**params)
        n += 1
    _print({"initialized": True, "home": str(Path(args.home).resolve()), "commands": n})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="water-compliance", description=__doc__)
    parser.add_argument("--home", help="数据目录（默认内存库，仅用于一次性演示）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="启动 HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("recompute", help="复算取水点任一时段可用量与实际履约")
    p.add_argument("point_id")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_recompute)

    p = sub.add_parser("section", help="复算生态控制断面逐时基流满足情况")
    p.add_argument("section_id")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_section)

    p = sub.add_parser("alerts", help="列出即将违约/已经透支/恢复合规区间")
    p.add_argument("point_id")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("evidence", help="按记录标识回溯告警/报告所依据的原始事件")
    p.add_argument("record_ids", nargs="+")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("assess", help="新增申请前评估对生态控制断面的影响")
    p.add_argument("point_id")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--volume-m3", type=float, required=True, dest="volume_m3")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("report-sign", help="签署月报（冻结快照）")
    p.add_argument("point_id")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.add_argument("--signed-by", required=True, dest="signed_by")
    p.set_defaults(func=cmd_report_sign)

    p = sub.add_parser("report-show", help="查看已签署月报（冻结正文）")
    p.add_argument("point_id")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.set_defaults(func=cmd_report_show)

    p = sub.add_parser("report-diff", help="已签署月报与当前重算的差异调整")
    p.add_argument("point_id")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.set_defaults(func=cmd_report_diff)

    p = sub.add_parser("import", help="从 JSON 场景文件批量导入命令（幂等）")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("events", help="列出哈希链上的全部事件")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("demo-init", help="初始化枯水期演示场景")
    p.add_argument("--force", action="store_true", help="目录已有数据时仍允许追加")
    p.set_defaults(func=cmd_demo_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ServiceError, IdempotencyConflict, ReportAlreadySigned) as exc:
        print(f"业务拒绝: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"不存在: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
