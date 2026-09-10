"""Unit tests for staff/leave_summary.py — per-staff leave-day counting
for a month (clipping leave periods to month boundaries) and the
'more than 2 days in a month' highlight rule."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from staff.leave_summary import month_bounds, summarize_month


@dataclass
class FakeStaff:
    id: int
    name: str


@dataclass
class FakeLeave:
    staff_id: int
    start_date: str
    end_date: str
    is_half_day: bool = False


def test_month_bounds_regular_month():
    start, end = month_bounds(2026, 8)
    assert start.isoformat() == "2026-08-01"
    assert end.isoformat() == "2026-08-31"


def test_month_bounds_december_rolls_to_next_year():
    start, end = month_bounds(2026, 12)
    assert start.isoformat() == "2026-12-01"
    assert end.isoformat() == "2026-12-31"


def test_single_day_leave_counts_as_one_day():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-08-10", "2026-08-10")]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 1
    assert result[0].highlighted is False


def test_leave_exceeding_two_days_is_highlighted():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-08-10", "2026-08-13")]  # 4 days
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 4
    assert result[0].highlighted is True


def test_exactly_two_days_not_highlighted():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-08-10", "2026-08-11")]  # exactly 2 days
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 2
    assert result[0].highlighted is False


def test_multiple_leaves_in_month_sum_together():
    staff = [FakeStaff(1, "Alice")]
    leaves = [
        FakeLeave(1, "2026-08-01", "2026-08-01"),
        FakeLeave(1, "2026-08-15", "2026-08-16"),
    ]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 3
    assert result[0].highlighted is True


def test_leave_spanning_month_boundary_is_clipped():
    # 2026-07-30 through 2026-08-02 -> only Aug 1-2 (2 days) count toward August.
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-07-30", "2026-08-02")]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 2
    assert result[0].highlighted is False

    result_july = summarize_month(staff, leaves, 2026, 7)
    assert result_july[0].days_on_leave == 2  # July 30-31


def test_leave_outside_month_contributes_zero():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-05-01", "2026-05-05")]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == 0
    assert result[0].highlighted is False


def test_staff_with_no_leaves_still_appears():
    staff = [FakeStaff(1, "Alice"), FakeStaff(2, "Bob")]
    leaves = [FakeLeave(1, "2026-08-10", "2026-08-13")]
    result = summarize_month(staff, leaves, 2026, 8)
    assert len(result) == 2
    bob = next(r for r in result if r.staff.name == "Bob")
    assert bob.days_on_leave == 0
    assert bob.highlighted is False


def test_half_day_leave_counts_as_half():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-08-10", "2026-08-10", is_half_day=True)]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == Decimal("0.5")
    assert result[0].highlighted is False


def test_half_day_leaves_sum_with_whole_days():
    staff = [FakeStaff(1, "Alice")]
    leaves = [
        FakeLeave(1, "2026-08-01", "2026-08-02"),  # 2 whole days
        FakeLeave(1, "2026-08-15", "2026-08-15", is_half_day=True),  # +0.5
    ]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == Decimal("2.5")
    assert result[0].highlighted is True  # 2.5 > 2


def test_two_half_days_do_not_sum_to_more_than_one_full_day():
    """Sanity check that half-day contributions aren't accidentally
    double-counted or rounded up."""
    staff = [FakeStaff(1, "Alice")]
    leaves = [
        FakeLeave(1, "2026-08-01", "2026-08-01", is_half_day=True),
        FakeLeave(1, "2026-08-02", "2026-08-02", is_half_day=True),
    ]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == Decimal("1.0")


def test_half_day_leave_outside_month_contributes_zero():
    staff = [FakeStaff(1, "Alice")]
    leaves = [FakeLeave(1, "2026-05-10", "2026-05-10", is_half_day=True)]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].days_on_leave == Decimal("0")


def test_results_sorted_most_days_first():
    staff = [FakeStaff(1, "Alice"), FakeStaff(2, "Bob")]
    leaves = [
        FakeLeave(1, "2026-08-01", "2026-08-01"),
        FakeLeave(2, "2026-08-01", "2026-08-05"),
    ]
    result = summarize_month(staff, leaves, 2026, 8)
    assert result[0].staff.name == "Bob"
    assert result[1].staff.name == "Alice"
