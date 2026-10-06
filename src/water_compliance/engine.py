"""逐时核算引擎（纯函数）。

所有结果都由 :class:`~water_compliance.projection.Projection` 现场重算得到，
不在任何地方缓存权威数字——这是"任一时段可复算"的保证。

口径约定（与 timeutil 一致）：全部为整点小时、左闭右开、立方米。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .projection import EffectivePermit, Projection
from .timeutil import HOUR, HourInterval, iso, month_key

COMPLIANT = "compliant"
NEAR_BREACH = "near_breach"
OVERDRAFT = "overdraft"
BREACH = "breach"  # 断面生态基流破坏

# 以最近 24 小时实际取水强度外推，结余不足以支撑 24 小时即判"即将违约"
RUNOUT_HORIZON_HOURS = 24


@dataclass(frozen=True, slots=True)
class HourlyPointRow:
    hour: str
    point_id: str
    permit_revision: str | None
    suspended: bool
    base_quota_m3: float
    temp_quota_m3: float
    transfer_in_m3: float
    transfer_out_m3: float
    total_quota_m3: float
    withdrawal_m3: float
    return_m3: float
    estimated: bool
    cumulative_quota_m3: float
    cumulative_withdrawal_m3: float
    cumulative_balance_m3: float
    status: str
    basis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StatusInterval:
    status: str
    start: str
    end: str
    hours: int
    peak_deficit_m3: float  # 区间内最大累计透支（compliant 区间为 0）


@dataclass(frozen=True, slots=True)
class SectionHourRow:
    hour: str
    section_id: str
    inflow_m3: float
    estimated: bool
    withdrawals_routed_m3: float
    returns_m3: float
    requirement_m3: float
    net_m3: float
    status: str
    basis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PointRecomputation:
    point_id: str
    interval: HourInterval
    rows: list[HourlyPointRow]
    status_intervals: list[StatusInterval]
    total_quota_m3: float
    total_withdrawal_m3: float
    total_return_m3: float
    total_estimated_m3: float
    basis: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "start": self.interval.start_text(),
            "end": self.interval.end_text(),
            "total_quota_m3": round(self.total_quota_m3, 3),
            "total_withdrawal_m3": round(self.total_withdrawal_m3, 3),
            "total_return_m3": round(self.total_return_m3, 3),
            "total_estimated_m3": round(self.total_estimated_m3, 3),
            "balance_m3": round(self.total_quota_m3 - self.total_withdrawal_m3, 3),
            "status_intervals": [
                {
                    "status": item.status,
                    "start": item.start,
                    "end": item.end,
                    "hours": item.hours,
                    "peak_deficit_m3": round(item.peak_deficit_m3, 3),
                }
                for item in self.status_intervals
            ],
            "hours": [_row_dict(row) for row in self.rows],
            "basis": list(self.basis),
        }


@dataclass(frozen=True, slots=True)
class ImpactHour:
    hour: str
    requirement_m3: float
    baseline_net_m3: float
    scenario_net_m3: float
    delta_m3: float
    baseline_status: str
    scenario_status: str
    basis: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ImpactAssessment:
    section_id: str
    point_id: str
    start: str
    end: str
    hourly_extra_m3: float
    baseline_breach_hours: int
    new_breach_hours: int
    worst_deficit_m3: float
    feasible: bool
    hours: list[ImpactHour]
    basis: tuple[str, ...]
    generated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "point_id": self.point_id,
            "start": self.start,
            "end": self.end,
            "hourly_extra_m3": round(self.hourly_extra_m3, 3),
            "baseline_breach_hours": self.baseline_breach_hours,
            "new_breach_hours": self.new_breach_hours,
            "worst_deficit_m3": round(self.worst_deficit_m3, 3),
            "feasible": self.feasible,
            "hours": [
                {
                    "hour": item.hour,
                    "requirement_m3": round(item.requirement_m3, 3),
                    "baseline_net_m3": round(item.baseline_net_m3, 3),
                    "scenario_net_m3": round(item.scenario_net_m3, 3),
                    "delta_m3": round(item.delta_m3, 3),
                    "baseline_status": item.baseline_status,
                    "scenario_status": item.scenario_status,
                    "basis": list(item.basis),
                }
                for item in self.hours
            ],
            "basis": list(self.basis),
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# 季节规则
# ---------------------------------------------------------------------------


def _md(year: int, mmdd: str) -> datetime:
    month, day = (int(part) for part in mmdd.split("-"))
    return datetime(year, month, day)


def _season_hourly(
    rules: tuple[tuple[str, str, str, float], ...], moment: datetime
) -> tuple[float, str | None]:
    """返回该小时的季节额度（季节总量均摊到季节内每小时）及季节编码。

    支持跨年度季节，如枯水期 11-01 ~ 次年 03-31：年初时刻属于上一年开始的
    季节窗口，年末时刻属于当年开始、下一年结束的窗口。
    """
    for code, start_md, end_md, total in rules:
        window = _matching_window(moment, start_md, end_md)
        if window is not None:
            return total / window.hours, code
    return 0.0, None


def _matching_window(
    moment: datetime, start_md: str, end_md: str
) -> HourInterval | None:
    if start_md <= end_md:
        window = HourInterval(
            _md(moment.year, start_md), _md(moment.year, end_md) + timedelta(days=1)
        )
        return window if window.contains(moment) else None
    # 跨年季节：11-01 ~ 03-31
    year_end_window = HourInterval(
        _md(moment.year, start_md), _md(moment.year + 1, end_md) + timedelta(days=1)
    )
    if year_end_window.contains(moment):
        return year_end_window
    year_start_window = HourInterval(
        _md(moment.year - 1, start_md), _md(moment.year, end_md) + timedelta(days=1)
    )
    if year_start_window.contains(moment):
        return year_start_window
    return None


def environmental_requirement(
    rules: tuple[tuple[str, str, str, float], ...], moment: datetime
) -> float:
    """断面生态基流：规则值即每小时最低下泄量（m³/h），不再均摊。"""
    for code, start_md, end_md, hourly_value in rules:
        if _matching_window(moment, start_md, end_md) is not None:
            return hourly_value
    return 0.0


# ---------------------------------------------------------------------------
# 取水点逐时核算
# ---------------------------------------------------------------------------


def recompute_point(
    projection: Projection, point_id: str, interval: HourInterval
) -> PointRecomputation:
    if point_id not in projection.points:
        raise KeyError(f"取水点不存在: {point_id}")

    meter = projection.effective_meter(point_id)
    returns = projection.effective_returns(point_id)
    transfers = projection.transfers()
    requests = projection.temporary_requests(point_id)

    rows: list[HourlyPointRow] = []
    cum_quota = cum_use = 0.0
    current_month: str | None = None
    recent_use: list[float] = []
    recent_quota: list[float] = []
    basis: set[str] = set()

    for moment in interval.each_hour():
        key = month_key(moment)
        if key != current_month:
            # 月度汇总是签署与重置口径；跨月重新累计
            current_month = key
            cum_quota = cum_use = 0.0
            recent_use = []
            recent_quota = []

        permit = projection.permit_at(point_id, moment)
        suspended = projection.is_suspended(point_id, moment)

        base = 0.0
        if permit is not None and not suspended:
            seasonal, _ = _season_hourly(permit.seasonal_quotas, moment)
            if seasonal > 0:
                base = seasonal
            else:
                # 无季节规则覆盖时按年度额度均摊
                base = permit.annual_quota_m3 / (366 if _is_leap(moment.year) else 365) / 24
            # 许可版本自身生效窗口之外不重复发放
            base = base if permit.interval.contains(moment) else 0.0

        temp_quota = 0.0
        for request in requests:
            if (
                request.decision == "approved"
                and request.interval.contains(moment)
            ):
                temp_quota += request.approved_extra_quota_m3 / request.interval.hours
                basis.add(f"request:{request.request_id}")

        transfer_in = transfer_out = 0.0
        for transfer in transfers:
            if transfer.interval.contains(moment):
                if transfer.to_point_id == point_id:
                    transfer_in += transfer.hourly_m3
                    basis.add(f"transfer:{transfer.transfer_id}")
                if transfer.from_point_id == point_id:
                    transfer_out += transfer.hourly_m3
                    basis.add(f"transfer:{transfer.transfer_id}")

        total_quota = base + temp_quota + transfer_in - transfer_out
        total_quota = max(total_quota, 0.0)

        entries = meter.get(moment, ())
        use = sum(entry.value_m3 for entry in entries)
        estimated = any(entry.source == "estimated" for entry in entries)
        entry_ids = tuple(entry.record_id for entry in entries)
        basis.update(entry_ids)
        if permit is not None:
            basis.add(f"permit:{point_id}:{permit.revision}")

        return_entries = returns.get(moment, ())
        returned = sum(entry.value_m3 for entry in return_entries)

        cum_quota += total_quota
        cum_use += use
        balance = cum_quota - cum_use

        recent_use = (recent_use + [use])[-RUNOUT_HORIZON_HOURS:]
        recent_quota = (recent_quota + [total_quota])[-RUNOUT_HORIZON_HOURS:]
        status = _classify(balance, recent_use, recent_quota, suspended, permit)

        rows.append(
            HourlyPointRow(
                hour=iso(moment),
                point_id=point_id,
                permit_revision=permit.revision if permit else None,
                suspended=suspended,
                base_quota_m3=round(base, 3),
                temp_quota_m3=round(temp_quota, 3),
                transfer_in_m3=round(transfer_in, 3),
                transfer_out_m3=round(transfer_out, 3),
                total_quota_m3=round(total_quota, 3),
                withdrawal_m3=round(use, 3),
                return_m3=round(returned, 3),
                estimated=estimated,
                cumulative_quota_m3=round(cum_quota, 3),
                cumulative_withdrawal_m3=round(cum_use, 3),
                cumulative_balance_m3=round(balance, 3),
                status=status,
                basis=entry_ids,
            )
        )

    intervals = _status_intervals(rows)
    return PointRecomputation(
        point_id=point_id,
        interval=interval,
        rows=rows,
        status_intervals=intervals,
        total_quota_m3=sum(row.total_quota_m3 for row in rows),
        total_withdrawal_m3=sum(row.withdrawal_m3 for row in rows),
        total_return_m3=sum(row.return_m3 for row in rows),
        total_estimated_m3=sum(
            row.withdrawal_m3 for row in rows if row.estimated
        ),
        basis=tuple(sorted(basis)),
    )


def _classify(
    balance: float,
    recent_use: list[float],
    recent_quota: list[float],
    suspended: bool,
    permit: EffectivePermit | None,
) -> str:
    if balance < -1e-9:
        return OVERDRAFT
    if permit is None or suspended:
        return COMPLIANT
    avg_use = sum(recent_use) / len(recent_use) if recent_use else 0.0
    avg_quota = sum(recent_quota) / len(recent_quota) if recent_quota else 0.0
    # 仅当近期取水强度超过配额强度（正在净消耗结余），且结余不足以再支撑 24 小时
    if avg_use > avg_quota + 1e-9 and balance < RUNOUT_HORIZON_HOURS * avg_use:
        return NEAR_BREACH
    return COMPLIANT


def _status_intervals(rows: list[HourlyPointRow]) -> list[StatusInterval]:
    if not rows:
        return []
    runs: list[StatusInterval] = []
    run_status = rows[0].status
    run_start = rows[0].hour
    peak = 0.0
    last_hour = rows[0].hour

    def close(end_hour: str, peak_deficit: float) -> None:
        start_dt = datetime.fromisoformat(run_start)
        end_dt = datetime.fromisoformat(end_hour) + HOUR
        runs.append(
            StatusInterval(
                status=run_status,
                start=run_start,
                end=iso(end_dt),
                hours=int((end_dt - start_dt) // HOUR),
                peak_deficit_m3=round(max(0.0, -peak_deficit), 3),
            )
        )

    for row in rows[1:]:
        if row.status != run_status:
            close(last_hour, peak)
            run_status = row.status
            run_start = row.hour
            peak = 0.0
        if row.cumulative_balance_m3 < peak:
            peak = row.cumulative_balance_m3
        last_hour = row.hour
    close(last_hour, peak)
    return runs


# ---------------------------------------------------------------------------
# 生态控制断面
# ---------------------------------------------------------------------------


def section_rows(
    projection: Projection, section_id: str, interval: HourInterval
) -> list[SectionHourRow]:
    if section_id not in projection.sections:
        raise KeyError(f"控制断面不存在: {section_id}")
    section = projection.sections[section_id]
    mapped_points = [
        point
        for point in projection.points.values()
        if point.control_section_id == section_id
    ]
    point_meter = {point.point_id: projection.effective_meter(point.point_id) for point in mapped_points}
    point_returns = {
        point.point_id: projection.effective_returns(point.point_id) for point in mapped_points
    }

    rows: list[SectionHourRow] = []
    inflow_series = projection.effective_section_inflows(section_id)
    for moment in interval.each_hour():
        requirement = environmental_requirement(section.environmental_rules, moment)
        routed = 0.0
        basis: list[str] = []
        for point in mapped_points:
            source_hour = moment - point.routing_lag_hours * HOUR
            for entry in point_meter[point.point_id].get(source_hour, ()):
                routed += entry.value_m3 * point.routing_factor
                basis.append(entry.record_id)
        returned = 0.0
        for point in mapped_points:
            for entry in point_returns[point.point_id].get(moment, ()):
                rec = projection.return_records.get(entry.original_record_id)
                if rec is not None and rec.to_section_id == section_id:
                    returned += entry.value_m3
                    basis.append(entry.record_id)

        inflow_entry = inflow_series.get(moment)
        inflow = inflow_entry.value_m3 if inflow_entry else 0.0
        estimated = bool(inflow_entry and inflow_entry.source == "estimated")
        if inflow_entry is not None:
            basis.append(inflow_entry.record_id)

        net = inflow + returned - routed
        rows.append(
            SectionHourRow(
                hour=iso(moment),
                section_id=section_id,
                inflow_m3=round(inflow, 3),
                estimated=estimated,
                withdrawals_routed_m3=round(routed, 3),
                returns_m3=round(returned, 3),
                requirement_m3=round(requirement, 3),
                net_m3=round(net, 3),
                status=COMPLIANT if net + 1e-9 >= requirement else BREACH,
                basis=tuple(basis),
            )
        )
    return rows


def assess_request_impact(
    projection: Projection,
    point_id: str,
    interval: HourInterval,
    volume_m3: float,
    generated_at: str,
) -> ImpactAssessment:
    """新增临时申请*之前*计算对下游生态控制断面的影响。

    情景 = 现状逐时取水 + 申请量均摊到申请时段每小时，经汇流滞后/系数到达断面。
    """
    point = projection.points[point_id]
    section_id = point.control_section_id
    hourly_extra = volume_m3 / interval.hours
    baseline = {
        datetime.fromisoformat(row.hour): row for row in section_rows(projection, section_id, interval)
    }

    hours: list[ImpactHour] = []
    new_breach = 0
    baseline_breach = 0
    worst = 0.0
    basis: set[str] = set()

    for moment in interval.each_hour():
        base_row = baseline[moment]
        source_hour = moment - point.routing_lag_hours * HOUR
        delta = hourly_extra * point.routing_factor if interval.contains(source_hour) else 0.0
        scenario_net = base_row.net_m3 - delta
        scenario_status = COMPLIANT if scenario_net + 1e-9 >= base_row.requirement_m3 else BREACH
        if base_row.status == BREACH:
            baseline_breach += 1
        if scenario_status == BREACH and base_row.status != BREACH:
            new_breach += 1
        deficit = base_row.requirement_m3 - scenario_net
        if deficit > worst:
            worst = deficit
        basis.update(base_row.basis)
        hours.append(
            ImpactHour(
                hour=iso(moment),
                requirement_m3=base_row.requirement_m3,
                baseline_net_m3=base_row.net_m3,
                scenario_net_m3=round(scenario_net, 3),
                delta_m3=round(delta, 3),
                baseline_status=base_row.status,
                scenario_status=scenario_status,
                basis=base_row.basis,
            )
        )

    return ImpactAssessment(
        section_id=section_id,
        point_id=point_id,
        start=interval.start_text(),
        end=interval.end_text(),
        hourly_extra_m3=hourly_extra,
        baseline_breach_hours=baseline_breach,
        new_breach_hours=new_breach,
        worst_deficit_m3=round(worst, 3),
        feasible=new_breach == 0,
        hours=hours,
        basis=tuple(sorted(basis)),
        generated_at=generated_at,
    )


def _row_dict(row: HourlyPointRow) -> dict[str, Any]:
    return {
        "hour": row.hour,
        "permit_revision": row.permit_revision,
        "suspended": row.suspended,
        "base_quota_m3": row.base_quota_m3,
        "temp_quota_m3": row.temp_quota_m3,
        "transfer_in_m3": row.transfer_in_m3,
        "transfer_out_m3": row.transfer_out_m3,
        "total_quota_m3": row.total_quota_m3,
        "withdrawal_m3": row.withdrawal_m3,
        "return_m3": row.return_m3,
        "estimated": row.estimated,
        "cumulative_quota_m3": row.cumulative_quota_m3,
        "cumulative_withdrawal_m3": row.cumulative_withdrawal_m3,
        "cumulative_balance_m3": row.cumulative_balance_m3,
        "status": row.status,
        "basis": list(row.basis),
    }


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
