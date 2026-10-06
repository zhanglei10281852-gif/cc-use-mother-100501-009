"""命令行：监管人员可直接复算任一时段并回溯告警依据。

用法示例::

    python -m water_compliance.cli --db water.db availability \
        --permit P-IND-01 --use industrial --hour 2026-10-05T14
    python -m water_compliance.cli --db water.db alerts \
        --start 2026-10-01T00 --end 2026-11-01T00
    python -m water_compliance.cli --db water.db import events.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .service import ComplianceService, ServiceError
from .storage import ContentConflict, Ledger


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True,
              default=lambda v: sorted(v) if isinstance(v, (set, frozenset)) else str(v))
    sys.stdout.write("\n")


def _service(args: argparse.Namespace) -> ComplianceService:
    return ComplianceService(Ledger(args.db))


def cmd_availability(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.availability(args.permit, args.use, args.hour))


def cmd_compliance(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.compliance(args.start, args.end))


def cmd_alerts(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.alert_basis(args.start, args.end))


def cmd_event(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        event = svc.event(args.seq)
        if event is None:
            raise SystemExit(f"事件 {args.seq} 不存在")
        _print(event)


def cmd_events(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print([e.to_dict() for e in svc.ledger.all_events()])


def cmd_evaluate(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.evaluate_application(
            args.permit, args.use, args.start, args.end, args.rate,
            priority_flag=args.priority))


def cmd_apply(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.submit_application(
            args.application_id, args.permit, args.use,
            args.start, args.end, args.rate,
            priority_flag=args.priority,
            observed_month=args.observed_month,
            auto_decide=not args.no_auto_decide))


def cmd_sign(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.sign_report(args.month, args.signed_by, args.doc_ref).__dict__)


def cmd_report(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.build_monthly_report(args.month))


def cmd_verify(args: argparse.Namespace) -> None:
    with _service_context(args) as svc:
        _print(svc.verify_report(args.month))


def cmd_post(args: argparse.Namespace) -> None:
    """直接追加一条类型化事件（高级用法，从 JSON 文件或字符串读取）。"""
    payload = json.loads(args.payload)
    with _service_context(args) as svc:
        result = svc._post(args.type, payload)  # noqa: SLF001
        _print(result.__dict__)


def cmd_import(args: argparse.Namespace) -> None:
    """批量导入 JSONL：每行 {"event_type": ..., "payload": ...}。

    同一原始记录重复导入不会多扣额度：幂等重复计入 duplicates 并回显既有 seq。
    """
    summary = {"imported": 0, "duplicates": 0, "conflicts": 0, "items": []}
    with _service_context(args) as svc:
        with open(args.file, encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    result = svc._post(item["event_type"], item["payload"])  # noqa: SLF001
                except KeyError as exc:
                    raise SystemExit(f"第 {line_no} 行缺少字段: {exc}")
                except ContentConflict as exc:
                    summary["conflicts"] += 1
                    summary["items"].append({"line": line_no, "conflict": str(exc)})
                    continue
                except ServiceError as exc:
                    raise SystemExit(f"第 {line_no} 行被规则拒绝: {exc}")
                if result.duplicate:
                    summary["duplicates"] += 1
                else:
                    summary["imported"] += 1
                summary["items"].append({"line": line_no, "seq": result.seq,
                                         "duplicate": result.duplicate,
                                         "event_type": result.event_type})
    _print(summary)


class _service_context:
    def __init__(self, args: argparse.Namespace):
        self.args = args

    def __enter__(self) -> ComplianceService:
        self.ledger = Ledger(self.args.db)
        return ComplianceService(self.ledger)

    def __exit__(self, *exc: object) -> None:
        self.ledger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生态用水履约核算命令行")
    parser.add_argument("--db", default="water.db", help="SQLite 台账路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("availability", help="复算某时点可用量")
    p.add_argument("--permit", required=True)
    p.add_argument("--use", required=True)
    p.add_argument("--hour", required=True)
    p.set_defaults(func=cmd_availability)

    p = sub.add_parser("compliance", help="复算窗口内逐时履约")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_compliance)

    p = sub.add_parser("alerts", help="输出违约/预警/恢复区间及依据记录")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("event", help="按 seq 查看台账事件")
    p.add_argument("--seq", type=int, required=True)
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("events", help="列出全部台账事件")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("evaluate", help="只评估申请，不入账")
    p.add_argument("--permit", required=True)
    p.add_argument("--use", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--priority", action="store_true")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("apply", help="登记申请并自动裁定")
    p.add_argument("--application-id", required=True)
    p.add_argument("--permit", required=True)
    p.add_argument("--use", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--priority", action="store_true")
    p.add_argument("--observed-month")
    p.add_argument("--no-auto-decide", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("sign", help="签署月报")
    p.add_argument("--month", required=True)
    p.add_argument("--signed-by", required=True)
    p.add_argument("--doc-ref", required=True)
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("report", help="构造但不签署月报")
    p.add_argument("--month", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("verify", help="复算并验签月报，列出签署后差异调整")
    p.add_argument("--month", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("post", help="追加类型化事件（高级）")
    p.add_argument("--type", required=True)
    p.add_argument("--payload", required=True, help="JSON 字符串")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("import", help="批量导入 JSONL 事件")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("serve", help="启动 HTTP JSON API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    return parser


def cmd_serve(args: argparse.Namespace) -> None:
    from .api import serve
    serve(args.host, args.port, args.db)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ServiceError as exc:
        raise SystemExit(f"业务错误: {exc}")


if __name__ == "__main__":
    main()
