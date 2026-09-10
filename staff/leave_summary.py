"""
Per-staff leave-day counting for a given calendar month, and the
">2 days in a month" highlight rule (spec: "if any staff take leaves more
than 2 days in a month, highlight that staff explicitly").

A StaffLeave row stores a [start_date, end_date] PERIOD, not one row per
day — so a leave spanning a month boundary must be clipped to the month
being summarized, not counted whole against both months (or against the
wrong one).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

# "more than 2 days" -> 3+ triggers the highlight, exactly 2 does not.
# Also correctly excludes e.g. 2.5 half-days from the highlight, which
# reads oddly ("2.5 > 2" would trigger it) but is intentional -- half a
# day over the threshold is still over it.
LEAVE_HIGHLIGHT_THRESHOLD_DAYS = 2


@dataclass
class StaffLeaveSummary:
    staff: Any  # db.models.Staff (or a fake with the same attributes, in tests)
    days_on_leave: Decimal  # whole days from ordinary leaves + 0.5 per half-day leave
    highlighted: bool


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, next_month - timedelta(days=1)


def _clipped_days(leave_start: date, leave_end: date, month_start: date, month_end: date) -> int:
    s = max(leave_start, month_start)
    e = min(leave_end, month_end)
    return (e - s).days + 1 if s <= e else 0


def summarize_month(staff_list: Iterable[Any], leaves: Iterable[Any], year: int, month: int) -> list[StaffLeaveSummary]:
    """staff_list: db.models.Staff rows to summarize (every one gets a
    result, even with 0 leave days, so a clean staff member is still
    visible in the table). leaves: db.models.StaffLeave rows — leaves for
    staff not in staff_list are ignored, not raised on."""
    month_start, month_end = month_bounds(year, month)

    days_by_staff_id: dict[int, Decimal] = {}
    for leave in leaves:
        leave_start = datetime.strptime(leave.start_date, "%Y-%m-%d").date()
        leave_end = datetime.strptime(leave.end_date, "%Y-%m-%d").date()
        days = _clipped_days(leave_start, leave_end, month_start, month_end)
        if days > 0:
            # A half-day leave is always a single day (enforced when it's
            # created/edited, not here) -- clipping it to the month either
            # keeps it whole (contributes 0.5) or drops it entirely (the
            # `days > 0` check above), never partially.
            contribution = Decimal("0.5") if getattr(leave, "is_half_day", False) else Decimal(days)
            days_by_staff_id[leave.staff_id] = days_by_staff_id.get(leave.staff_id, Decimal("0")) + contribution

    results = [
        StaffLeaveSummary(
            staff=s,
            days_on_leave=days_by_staff_id.get(s.id, Decimal("0")),
            highlighted=days_by_staff_id.get(s.id, Decimal("0")) > LEAVE_HIGHLIGHT_THRESHOLD_DAYS,
        )
        for s in staff_list
    ]
    results.sort(key=lambda r: r.days_on_leave, reverse=True)
    return results
