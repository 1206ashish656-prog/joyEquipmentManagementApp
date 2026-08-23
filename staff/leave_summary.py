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
from typing import Any, Iterable

# "more than 2 days" -> 3+ triggers the highlight, exactly 2 does not.
LEAVE_HIGHLIGHT_THRESHOLD_DAYS = 2


@dataclass
class StaffLeaveSummary:
    staff: Any  # db.models.Staff (or a fake with the same attributes, in tests)
    days_on_leave: int
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

    days_by_staff_id: dict[int, int] = {}
    for leave in leaves:
        leave_start = datetime.strptime(leave.start_date, "%Y-%m-%d").date()
        leave_end = datetime.strptime(leave.end_date, "%Y-%m-%d").date()
        days = _clipped_days(leave_start, leave_end, month_start, month_end)
        if days > 0:
            days_by_staff_id[leave.staff_id] = days_by_staff_id.get(leave.staff_id, 0) + days

    results = [
        StaffLeaveSummary(
            staff=s,
            days_on_leave=days_by_staff_id.get(s.id, 0),
            highlighted=days_by_staff_id.get(s.id, 0) > LEAVE_HIGHLIGHT_THRESHOLD_DAYS,
        )
        for s in staff_list
    ]
    results.sort(key=lambda r: r.days_on_leave, reverse=True)
    return results
