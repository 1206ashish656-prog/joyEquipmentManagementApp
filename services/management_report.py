"""
Senior-management report (admin-only, /reports/management — spec: an
aggregated + monthly breakdown of sales/revenue/cost/profit, venue
performance ranked by sales volume, and per-machine downtime broken
down by time of day). Pure data computation, run only from
services/report_job_worker.py's background loop (per explicit request,
"an isolated report generation process") — this module has no FastAPI
or Playwright dependency of its own.

Downtime is sourced from FaultLogHistory (db/models.py), backfilled
from the target application's own historical Fault Information log
(monitoring/fault_log_backfill.py) rather than from FaultIncident —
per explicit request to backfill historical dates, since FaultIncident
only ever has data from whenever this app's own polling started
running, while the target's own log has real historical depth. See
_compute_downtime()'s docstring for the exact rule (is_stop=True rows
only, overlapping component faults merged into one span so simultaneous
faults on one machine don't double-count).

Cost/profit is a company-wide figure only: CostEntry has no per-venue
or per-equipment link at all (Rent is tied to a Venue, but Oranges/
Glass/Straws/etc. aren't tied to anything), so there is no honest way
to attribute cost (and therefore profit) to one machine or one venue
without fabricating an allocation. Venue performance is ranked by SALES
VOLUME ONLY (explicit requirement: "based on sales number") — never
profit — which sidesteps that gap entirely; revenue is shown alongside
orders per venue (a separate explicit requirement) but never drives the
Outperforming/Underperforming call. When the report is scoped to a
single machine (equipment_id given), cost/profit is reported as "not
available" rather than silently showing a wrong (company-wide) number
next to that one machine's sales.

Both a monthly (`monthly`) and a daily (`daily`) orders/revenue
breakdown are always computed — backend/api/reports.py picks which one
feeds the "sales over time" chart based on the selected period (daily
granularity for weekly/monthly, monthly granularity otherwise), per
explicit request. This module stays UI-agnostic about that choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import CostEntry, Equipment, FaultLogHistory, OrderSummary, VenueMapping
from orders.mapping import IST_TZ
from orders.rollup import rollup as order_rollup

TIME_OF_DAY_BUCKETS = ("Morning", "Afternoon", "Evening", "Night")
# (bucket name, start hour, end hour) within one IST calendar day. Night
# is split into two sub-intervals (before 06:00 and from 21:00) so every
# hour of the day maps to exactly one bucket without any midnight-
# wraparound arithmetic -- see _time_of_day_seconds.
_DAY_SUBINTERVALS = (
    ("Night", 0, 6), ("Morning", 6, 12), ("Afternoon", 12, 17), ("Evening", 17, 21), ("Night", 21, 24),
)


@dataclass
class MonthlyRow:
    month: str  # 'YYYY-MM'
    orders: int
    revenue: Decimal
    cost: Decimal | None  # None when cost isn't available (single-machine scope)

    @property
    def profit(self) -> Decimal | None:
        return None if self.cost is None else self.revenue - self.cost


@dataclass
class DailyRow:
    date: str  # 'YYYY-MM-DD'
    orders: int
    revenue: Decimal


@dataclass
class VenuePerformance:
    venue: str
    orders: int
    revenue: Decimal
    performance: str  # "Outperforming" | "Underperforming" | "Average"


@dataclass
class MachineDowntime:
    equipment_name: str
    total_seconds: float
    by_time_of_day: dict[str, float] = field(default_factory=dict)


@dataclass
class ManagementReport:
    start: str
    end: str
    equipment_scope: str  # equipment name, or "All Machines"
    cost_profit_available: bool
    total_orders: int
    total_revenue: Decimal
    total_cost: Decimal | None
    monthly: list[MonthlyRow]
    daily: list[DailyRow]
    venue_performance: list[VenuePerformance]  # empty when scoped to one machine
    downtime: list[MachineDowntime]

    @property
    def total_profit(self) -> Decimal | None:
        return None if self.total_cost is None else self.total_revenue - self.total_cost


def format_duration(seconds: float) -> str:
    """Same "biggest two units" shape as services/fault_digest.py's
    _duration() (days+hours, or hours+minutes, or just minutes) --
    registered as a Jinja filter (backend/templating.py) for the report
    templates."""
    total_minutes = int(seconds // 60)
    days, remainder_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder_minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _month_key(date_str: str) -> str:
    return date_str[:7]  # 'YYYY-MM-DD' -> 'YYYY-MM'


def _months_between(start: str, end: str) -> list[str]:
    """Every 'YYYY-MM' the [start, end] date range touches, in order --
    so a month with zero orders/costs still gets its own zero row rather
    than silently disappearing from the breakdown."""
    s = datetime.strptime(start, "%Y-%m-%d").date().replace(day=1)
    e = datetime.strptime(end, "%Y-%m-%d").date().replace(day=1)
    months = []
    cur = s
    while cur <= e:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


def _dates_between(start: str, end: str) -> list[str]:
    """Every 'YYYY-MM-DD' the [start, end] range touches, in order --
    same zero-filling rationale as _months_between, at day granularity
    for the "sales over time" chart when a short (weekly/monthly)
    period is selected (backend/api/reports.py decides which
    granularity to chart; this always computes both)."""
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    dates = []
    cur = s
    while cur <= e:
        dates.append(cur.isoformat())
        cur += timedelta(days=1)
    return dates


def _time_of_day_seconds(seg_start: datetime, seg_end: datetime) -> dict[str, float]:
    """Splits [seg_start, seg_end] (aware UTC datetimes) into
    Morning/Afternoon/Evening/Night seconds, in IST (this app's
    established reporting timezone -- orders/mapping.py's IST_TZ), by
    walking each IST calendar day the interval touches and intersecting
    it with that day's five fixed sub-intervals. Correctly handles a
    downtime interval spanning multiple days (rare but possible for a
    long OFFLINE incident)."""
    buckets = {b: 0.0 for b in TIME_OF_DAY_BUCKETS}
    if seg_end <= seg_start:
        return buckets

    start_ist = seg_start.astimezone(IST_TZ)
    end_ist = seg_end.astimezone(IST_TZ)
    day = start_ist.replace(hour=0, minute=0, second=0, microsecond=0)

    while day < end_ist:
        for name, h0, h1 in _DAY_SUBINTERVALS:
            window_start = day + timedelta(hours=h0)
            window_end = day + timedelta(hours=h1)
            overlap_start = max(window_start, start_ist)
            overlap_end = min(window_end, end_ist)
            if overlap_end > overlap_start:
                buckets[name] += (overlap_end - overlap_start).total_seconds()
        day += timedelta(days=1)

    return buckets


def _as_aware_utc(value: datetime) -> datetime:
    """SQLite (used by tests) can drop tzinfo on round-trip; Postgres
    never does. Treat a naive value as UTC either way -- same defensive
    pattern already used by orders/mapping.py's format_ist and
    services/fault_digest.py's _duration."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _venue_lookup(db: Session) -> dict[str, str]:
    """machine_name (lowercased) -> venue_provider, for mapping
    OrderSummary.device_app to a venue. Case-insensitive because the
    target app isn't consistent about machine-name casing (same
    precedent as backend/api/orders.py's _venue_machines)."""
    rows = db.execute(select(VenueMapping.machine_name, VenueMapping.venue_provider)).all()
    return {machine.lower(): venue for machine, venue in rows}


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Collapses overlapping/touching [start, end) intervals into their
    union -- two components failing on the same machine at overlapping
    times must count as one span of actual machine downtime, not two
    separately-summed durations (that would double-count the overlap and
    overstate how long the machine was really down)."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _compute_downtime(
    db: Session, start: str, end: str, equipment_ids: list[int] | None,
) -> list[MachineDowntime]:
    """Sourced entirely from FaultLogHistory (backfilled from the
    target's own Fault Information log, monitoring/fault_log_backfill.py)
    rather than FaultIncident -- see db/models.py's FaultLogHistory
    docstring for why: this has real historical depth, FaultIncident
    only has data from whenever this app's own polling started. Only
    is_stop=True rows count as downtime -- a component fault that never
    actually stopped the machine (is_stop=False) isn't downtime."""
    period_start = datetime.combine(datetime.strptime(start, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=IST_TZ).astimezone(timezone.utc)
    period_end_exclusive = (
        datetime.combine(datetime.strptime(end, "%Y-%m-%d").date(), datetime.min.time(), tzinfo=IST_TZ) + timedelta(days=1)
    ).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)

    query = select(FaultLogHistory, Equipment).join(Equipment, FaultLogHistory.equipment_id == Equipment.id).where(
        FaultLogHistory.occurred_at < period_end_exclusive,
        FaultLogHistory.is_stop.is_(True),
    )
    if equipment_ids is not None:
        query = query.where(FaultLogHistory.equipment_id.in_(equipment_ids))
    log_rows = db.execute(query).all()

    intervals_by_equipment: dict[str, list[tuple[datetime, datetime]]] = {}
    for log, equipment in log_rows:
        # A fault the target hasn't cleared yet (is_clean=False, no
        # cleared_at) counts as ongoing through "now" -- its downtime-
        # so-far still belongs in the report if it overlaps the period.
        log_end = _as_aware_utc(log.cleared_at) if log.cleared_at else now
        log_start = _as_aware_utc(log.occurred_at)

        seg_start = max(log_start, period_start)
        seg_end = min(log_end, period_end_exclusive)
        if seg_end <= seg_start:
            continue

        intervals_by_equipment.setdefault(equipment.name, []).append((seg_start, seg_end))

    downtime: list[MachineDowntime] = []
    for equipment_name, intervals in intervals_by_equipment.items():
        merged = _merge_intervals(intervals)
        entry = MachineDowntime(equipment_name=equipment_name, total_seconds=0.0)
        for seg_start, seg_end in merged:
            entry.total_seconds += (seg_end - seg_start).total_seconds()
            for bucket, seconds in _time_of_day_seconds(seg_start, seg_end).items():
                entry.by_time_of_day[bucket] = entry.by_time_of_day.get(bucket, 0.0) + seconds
        downtime.append(entry)

    return sorted(downtime, key=lambda d: d.total_seconds, reverse=True)


def build_report(db: Session, start: str, end: str, equipment_id: int | None) -> ManagementReport:
    scoped_equipment: Equipment | None = None
    if equipment_id is not None:
        scoped_equipment = db.get(Equipment, equipment_id)

    order_query = select(OrderSummary).where(OrderSummary.date >= start, OrderSummary.date <= end)
    if scoped_equipment is not None:
        order_query = order_query.where(OrderSummary.device_app == scoped_equipment.name)
    order_rows = db.execute(order_query).scalars().all()

    overall_orders = order_rollup(order_rows, group_by=())
    total_orders = overall_orders[0].number_of_orders if overall_orders else 0
    total_revenue = overall_orders[0].revenue if overall_orders else Decimal("0.00")

    # Cost/profit: company-wide only -- see module docstring.
    cost_profit_available = scoped_equipment is None
    total_cost: Decimal | None = None
    cost_by_month: dict[str, Decimal] = {}
    if cost_profit_available:
        cost_rows = db.execute(select(CostEntry).where(CostEntry.date >= start, CostEntry.date <= end)).scalars().all()
        total_cost = sum((c.amount for c in cost_rows), Decimal("0.00"))
        for c in cost_rows:
            key = _month_key(c.date)
            cost_by_month[key] = cost_by_month.get(key, Decimal("0.00")) + c.amount

    orders_by_month: dict[str, list[OrderSummary]] = {}
    orders_by_date: dict[str, list[OrderSummary]] = {}
    for row in order_rows:
        orders_by_month.setdefault(_month_key(row.date), []).append(row)
        orders_by_date.setdefault(row.date, []).append(row)

    monthly: list[MonthlyRow] = []
    for month in _months_between(start, end):
        month_orders = orders_by_month.get(month, [])
        month_rollup = order_rollup(month_orders, group_by=())
        monthly.append(MonthlyRow(
            month=month,
            orders=month_rollup[0].number_of_orders if month_rollup else 0,
            revenue=month_rollup[0].revenue if month_rollup else Decimal("0.00"),
            cost=cost_by_month.get(month, Decimal("0.00")) if cost_profit_available else None,
        ))

    daily: list[DailyRow] = []
    for day in _dates_between(start, end):
        day_orders = orders_by_date.get(day, [])
        day_rollup = order_rollup(day_orders, group_by=())
        daily.append(DailyRow(
            date=day,
            orders=day_rollup[0].number_of_orders if day_rollup else 0,
            revenue=day_rollup[0].revenue if day_rollup else Decimal("0.00"),
        ))

    # Venue performance: sales-volume ranking only -- meaningless (and
    # not computed) when the report is already scoped to one machine.
    # Revenue is shown alongside orders (explicit requirement) but never
    # drives the Outperforming/Underperforming call itself.
    venue_performance: list[VenuePerformance] = []
    if scoped_equipment is None:
        lookup = _venue_lookup(db)
        orders_per_venue: dict[str, int] = {}
        revenue_per_venue: dict[str, Decimal] = {}
        for row in order_rollup(order_rows, group_by=("device_app",)):
            venue = lookup.get(row.key["device_app"].lower(), "Unmapped")
            orders_per_venue[venue] = orders_per_venue.get(venue, 0) + row.number_of_orders
            revenue_per_venue[venue] = revenue_per_venue.get(venue, Decimal("0.00")) + row.revenue

        if orders_per_venue:
            average = sum(orders_per_venue.values()) / len(orders_per_venue)
            for venue, orders in sorted(orders_per_venue.items(), key=lambda kv: kv[1], reverse=True):
                if orders > average:
                    performance = "Outperforming"
                elif orders < average:
                    performance = "Underperforming"
                else:
                    performance = "Average"
                venue_performance.append(VenuePerformance(
                    venue=venue, orders=orders, revenue=revenue_per_venue[venue], performance=performance,
                ))

    equipment_ids = [scoped_equipment.id] if scoped_equipment is not None else None
    downtime = _compute_downtime(db, start, end, equipment_ids)

    return ManagementReport(
        start=start,
        end=end,
        equipment_scope=scoped_equipment.name if scoped_equipment is not None else "All Machines",
        cost_profit_available=cost_profit_available,
        total_orders=total_orders,
        total_revenue=total_revenue,
        total_cost=total_cost,
        monthly=monthly,
        daily=daily,
        venue_performance=venue_performance,
        downtime=downtime,
    )
