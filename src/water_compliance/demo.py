"""枯水期演示场景：以命令列表描述，可被 CLI demo-init / 测试复用。

时间线（2026-11-01 起 240 小时）：
- 断面 S1 枯水期生态基流 500 m³/h，天然来水 600 m³/h；
- 工业园 P-IND 许可 50 m³/h：前 48h 用 40（合规），随后 48h 用 120
  （先即将违约、后透支，且同期断面基流破坏），之后降至 20（恢复合规）；
- 灌区 P-IRR 许可 10 m³/h，h100~120 被暂停，h120~144 停泵并向工业园调剂；
- 城市供水 P-CITY 许可 30 m³/h，优先保障居民生活；
- h60 计量缺测先以调度台账暂估 120，月报签署后补报实测 125，只产生差异调整；
- 工业园 h200 起申请临时增量 100 m³/h×24h，评估显示新增 24 个基流破坏小时，被拒绝。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .timeutil import BEIJING, HOUR

START = datetime(2026, 11, 1, 0, 0, tzinfo=BEIJING)
HOURS = 240


def _h(offset: int) -> str:
    from .timeutil import iso

    return iso(START + offset * HOUR)


def _industrial_use(offset: int) -> float:
    if offset < 48:
        return 40.0
    if offset < 96:
        return 120.0
    return 20.0


def build_demo_commands() -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = [
        {
            "op": "register_user",
            "user_id": "U-IND",
            "name": "河东工业园",
            "kind": "industrial_park",
        },
        {
            "op": "register_user",
            "user_id": "U-IRR",
            "name": "西河灌区",
            "kind": "irrigation_district",
        },
        {
            "op": "register_user",
            "user_id": "U-CITY",
            "name": "第三城市水厂",
            "kind": "urban_supply",
        },
        {
            "op": "register_section",
            "section_id": "S1",
            "name": "下游生态控制断面",
            "environmental_rules": [
                ["dry", "11-01", "03-31", 500.0],
                ["wet", "04-01", "10-31", 300.0],
            ],
        },
        {
            "op": "register_point",
            "point_id": "P-IND",
            "name": "工业园取水口",
            "user_id": "U-IND",
            "river_reach": "东河K12",
            "control_section_id": "S1",
        },
        {
            "op": "register_point",
            "point_id": "P-IRR",
            "name": "灌区总干渠首",
            "user_id": "U-IRR",
            "river_reach": "西河汇合口",
            "control_section_id": "S1",
            "routing_lag_hours": 2,
            "routing_factor": 0.95,
        },
        {
            "op": "register_point",
            "point_id": "P-CITY",
            "name": "水厂取水泵站",
            "user_id": "U-CITY",
            "river_reach": "东河K09",
            "control_section_id": "S1",
        },
    ]

    permits = [
        ("P-IND", "V1", 1_440_000.0, ["production"], ["工业生产"], [["dry", "11-01", "11-30", 36_000.0]]),
        ("P-IRR", "V1", 480_000.0, ["agriculture"], ["农业灌溉"], [["dry", "11-01", "11-30", 7_200.0]]),
        ("P-CITY", "V1", 900_000.0, ["domestic"], ["居民生活"], [["dry", "11-01", "11-30", 21_600.0]]),
    ]
    for point_id, revision, annual, purposes, priority, seasonal in permits:
        commands.append(
            {
                "op": "add_permit_version",
                "point_id": point_id,
                "revision": revision,
                "valid_from": "2026-01-01T00:00+08:00",
                "annual_quota_m3": annual,
                "purpose_codes": purposes,
                "priority_subjects": priority,
                "seasonal_quotas": seasonal,
                "dry_season_months": [11, 12, 1, 2, 3],
            }
        )

    # 逐时断面来水
    for offset in range(HOURS):
        commands.append(
            {
                "op": "record_section_inflow",
                "record_id": f"inflow-{offset:03d}",
                "section_id": "S1",
                "hour": _h(offset),
                "inflow_m3": 600.0,
            }
        )

    # 工业园逐时计量；h60 缺测暂估
    for offset in range(HOURS):
        use = _industrial_use(offset)
        if offset == 60:
            commands.append(
                {
                    "op": "record_meter",
                    "record_id": "meter-ind-060-est",
                    "point_id": "P-IND",
                    "hour": _h(offset),
                    "withdrawal_m3": 120.0,
                    "source": "estimated",
                    "estimate_source": "泵站运行台账推算（调度令〔2026〕枯17号）",
                    "note": "流量计缺测，暂估待补报",
                }
            )
            continue
        commands.append(
            {
                "op": "record_meter",
                "record_id": f"meter-ind-{offset:03d}",
                "point_id": "P-IND",
                "hour": _h(offset),
                "withdrawal_m3": use,
            }
        )

    # 灌区：h100~144 停泵（含暂停窗口与调剂窗口）
    for offset in range(HOURS):
        use = 0.0 if 100 <= offset < 144 else 10.0
        commands.append(
            {
                "op": "record_meter",
                "record_id": f"meter-irr-{offset:03d}",
                "point_id": "P-IRR",
                "hour": _h(offset),
                "withdrawal_m3": use,
            }
        )

    # 城市供水逐时计量与少量退水
    for offset in range(HOURS):
        commands.append(
            {
                "op": "record_meter",
                "record_id": f"meter-city-{offset:03d}",
                "point_id": "P-CITY",
                "hour": _h(offset),
                "withdrawal_m3": 30.0,
            }
        )
        if offset % 24 == 0:
            commands.append(
                {
                    "op": "record_return",
                    "record_id": f"return-city-{offset:03d}",
                    "point_id": "P-CITY",
                    "hour": _h(offset),
                    "return_m3": 120.0,
                    "to_section_id": "S1",
                }
            )

    commands += [
        {
            "op": "suspend_permit",
            "point_id": "P-IRR",
            "revision": "V1",
            "suspend_from": _h(100),
            "suspend_to": _h(120),
            "reason": "枯水期应急调度，暂停灌区取水许可",
            "enforcement_ref": "执停〔2026〕03号",
        },
        {
            "op": "approve_transfer",
            "transfer_id": "T-001",
            "from_point_id": "P-IRR",
            "to_point_id": "P-IND",
            "valid_from": _h(120),
            "valid_to": _h(144),
            "volume_m3": 120.0,
            "approval_ref": "调字〔2026〕08号",
            "purpose_code": "production",
        },
    ]

    # 工业园临时增量申请：先评估（评估由 API/CLI 在提交前生成，这里记录提交与决定）
    # 演示中由服务流程保证；场景文件直接给出与 CLI 同口径生成的评估编号占位，
    # 实际使用时应先调 assess_impact。此处不包含 submit_request，
    # 交由测试与 README 演示完整"评估→提交→决定"链路。

    commands += [
        {
            "op": "log_review",
            "review_id": "R-001",
            "point_id": "P-IND",
            "valid_from": _h(55),
            "valid_to": _h(192),
            "conclusion": "confirmed_violation",
            "reviewer": "执法监督员-王",
            "note": "枯水期连续超许可取水，同步造成断面基流破坏",
        },
        {
            "op": "log_appeal",
            "appeal_id": "A-001",
            "review_id": "R-001",
            "decision": "upheld",
            "decided_by": "水行政复议委员会",
            "note": "申诉理由不成立，维持违规定性",
        },
    ]

    return commands
