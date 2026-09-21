"""
Unit tests for services/management_report.py -- pure DB logic, no HTTP.
Uses the shared db_session fixture (tests/conftest.py, in-memory SQLite).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from db.models import CostEntry, Equipment, FaultLogHistory, OrderSummary, VenueMapping
from orders.mapping import IST_TZ
from services.management_report import (
    _merge_intervals,
    _months_between,
    _time_of_day_seconds,
    build_report,
    build_vendor_report,
    format_duration,
)


# --- format_duration ---

def test_format_duration_minutes_only():
    assert format_duration(5 * 60) == "5m"


def test_format_duration_hours_and_minutes():
    assert format_duration(3 * 3600 + 20 * 60) == "3h 20m"


def test_format_duration_days_and_hours():
    assert format_duration(2 * 86400 + 5 * 3600) == "2d 5h"


def test_format_duration_zero():
    assert format_duration(0) == "0m"


# --- _months_between ---

def test_months_between_single_month():
    assert _months_between("2026-08-01", "2026-08-31") == ["2026-08"]


def test_months_between_spans_year_boundary():
    assert _months_between("2026-11-15", "2027-01-10") == ["2026-11", "2026-12", "2027-01"]


# --- _time_of_day_seconds ---

def _ist(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST_TZ).astimezone(timezone.utc)


def test_time_of_day_fully_within_morning():
    buckets = _time_of_day_seconds(_ist(2026, 8, 15, 7), _ist(2026, 8, 15, 9))
    assert buckets["Morning"] == 2 * 3600
    assert buckets["Afternoon"] == 0
    assert buckets["Evening"] == 0
    assert buckets["Night"] == 0


def test_time_of_day_spans_two_buckets():
    buckets = _time_of_day_seconds(_ist(2026, 8, 15, 11, 30), _ist(2026, 8, 15, 12, 30))
    assert buckets["Morning"] == 30 * 60
    assert buckets["Afternoon"] == 30 * 60


def test_time_of_day_night_wraps_across_midnight_correctly():
    """23:00 -> 01:00 spans two calendar days but both halves are
    "Night" -- the two-sub-interval trick must merge them, not split
    Night into two separate reported buckets."""
    buckets = _time_of_day_seconds(_ist(2026, 8, 15, 23), _ist(2026, 8, 16, 1))
    assert buckets["Night"] == 2 * 3600
    assert buckets["Morning"] == 0


def test_time_of_day_empty_interval_is_zero():
    t = _ist(2026, 8, 15, 10)
    buckets = _time_of_day_seconds(t, t)
    assert all(v == 0 for v in buckets.values())


# --- _merge_intervals ---

def test_merge_intervals_empty_list():
    assert _merge_intervals([]) == []


def test_merge_intervals_non_overlapping_stay_separate():
    a, b = _ist(2026, 8, 15, 10), _ist(2026, 8, 15, 11)
    c, d = _ist(2026, 8, 15, 12), _ist(2026, 8, 15, 13)
    assert _merge_intervals([(a, b), (c, d)]) == [(a, b), (c, d)]


def test_merge_intervals_overlapping_collapse_to_union():
    a = _ist(2026, 8, 15, 10)
    b = _ist(2026, 8, 15, 10, 30)
    c = _ist(2026, 8, 15, 10, 15)
    d = _ist(2026, 8, 15, 10, 45)
    assert _merge_intervals([(a, b), (c, d)]) == [(a, d)]


def test_merge_intervals_touching_intervals_merge():
    a, b = _ist(2026, 8, 15, 10), _ist(2026, 8, 15, 11)
    c, d = _ist(2026, 8, 15, 11), _ist(2026, 8, 15, 12)
    assert _merge_intervals([(a, b), (c, d)]) == [(a, d)]


def test_merge_intervals_unordered_input_still_merges_correctly():
    a, b = _ist(2026, 8, 15, 10), _ist(2026, 8, 15, 11)
    c, d = _ist(2026, 8, 15, 9), _ist(2026, 8, 15, 10, 30)
    # Given out of order, the merged result must still be correct.
    assert _merge_intervals([(a, b), (c, d)]) == [(c, b)]


# --- build_report ---

def _add_equipment(db, name, external_id) -> Equipment:
    eq = Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name)
    db.add(eq)
    db.flush()
    return eq


def _add_order_summary(db, date, device_app, price="120.00", pay_type="UPI", n=10, oranges=25, weight="200.00"):
    db.add(OrderSummary(
        date=date, device_app=device_app, price=Decimal(price), pay_type=pay_type,
        number_of_orders=n, total_number_of_oranges=oranges, average_juice_weight=Decimal(weight),
    ))


def _add_cost(db, date, category, amount, vendor=None, item=""):
    db.add(CostEntry(date=date, category=category, vendor_name=vendor, item_name=item, amount=Decimal(amount)))


_next_target_log_id = [1000]


def _add_fault_log(db, equipment_id, occurred_at, cleared_at=None, is_stop=True):
    _next_target_log_id[0] += 1
    db.add(FaultLogHistory(
        equipment_id=equipment_id, target_log_id=_next_target_log_id[0],
        component_code="test_component", component_description="Test Component Malfunction",
        is_stop=is_stop, is_clean=cleared_at is not None,
        occurred_at=occurred_at, cleared_at=cleared_at,
    ))


def test_build_report_aggregates_orders_and_cost(db_session):
    _add_equipment(db_session, "NEXUS", "205")
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=10)
    _add_order_summary(db_session, "2026-08-20", "NEXUS", n=5)
    _add_cost(db_session, "2026-08-05", "Oranges", "1000.00")
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert report.total_orders == 15
    assert report.total_revenue == Decimal("1800.00")  # 15 * 120
    assert report.cost_profit_available is True
    assert report.total_cost == Decimal("1000.00")
    assert report.total_profit == Decimal("800.00")


def test_build_report_monthly_breakdown_includes_zero_months(db_session):
    _add_equipment(db_session, "NEXUS", "205")
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=10)
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-09-30", equipment_id=None)

    months = {m.month: m.orders for m in report.monthly}
    assert months == {"2026-08": 10, "2026-09": 0}


def test_build_report_daily_breakdown_includes_zero_days(db_session):
    """The daily granularity used for the "sales over time" chart when
    a weekly/monthly period is selected (backend/api/reports.py) --
    every date in range gets a row, even a zero one."""
    _add_equipment(db_session, "NEXUS", "205")
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=10)
    db_session.flush()

    report = build_report(db_session, "2026-08-09", "2026-08-11", equipment_id=None)

    days = {d.date: d.orders for d in report.daily}
    assert days == {"2026-08-09": 0, "2026-08-10": 10, "2026-08-11": 0}
    assert next(d.revenue for d in report.daily if d.date == "2026-08-10") == Decimal("1200.00")


def test_build_report_scoped_to_one_machine_excludes_cost_and_venues(db_session):
    eq = _add_equipment(db_session, "NEXUS", "205")
    _add_equipment(db_session, "Gravity", "116")
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=10)
    _add_order_summary(db_session, "2026-08-10", "Gravity", n=99)
    _add_cost(db_session, "2026-08-05", "Oranges", "1000.00")
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=eq.id)

    assert report.equipment_scope == "NEXUS"
    assert report.total_orders == 10  # only NEXUS, not Gravity's 99
    assert report.cost_profit_available is False
    assert report.total_cost is None
    assert report.total_profit is None
    assert report.venue_performance == []
    assert all(m.cost is None for m in report.monthly)


def test_build_report_venue_performance_ranks_by_sales(db_session):
    _add_equipment(db_session, "NEXUS", "205")
    _add_equipment(db_session, "Gravity", "116")
    db_session.add(VenueMapping(machine_name="NEXUS", venue_provider="Venue A"))
    db_session.add(VenueMapping(machine_name="Gravity", venue_provider="Venue B"))
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=100)
    _add_order_summary(db_session, "2026-08-10", "Gravity", n=10)
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    by_revenue = {v.venue: v.revenue for v in report.venue_performance}
    assert by_revenue["Venue A"] == Decimal("12000.00")  # 100 * 120
    assert by_revenue["Venue B"] == Decimal("1200.00")  # 10 * 120

    by_venue = {v.venue: v.performance for v in report.venue_performance}
    assert by_venue["Venue A"] == "Outperforming"
    assert by_venue["Venue B"] == "Underperforming"


def test_build_report_unmapped_machine_falls_back_to_unmapped_venue(db_session):
    _add_equipment(db_session, "NEXUS", "205")
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=10)
    db_session.flush()  # no VenueMapping row at all

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert len(report.venue_performance) == 1
    assert report.venue_performance[0].venue == "Unmapped"


def test_build_report_downtime_clipped_to_period(db_session):
    """A fault starting before the period and cleared partway through
    must only count the overlapping portion -- same clipping idiom as
    staff/leave_summary.py's month-boundary handling."""
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    _add_fault_log(
        db_session, eq.id,
        occurred_at=period_start_utc - timedelta(hours=2),  # started before the period
        cleared_at=period_start_utc + timedelta(hours=3),  # cleared 3h into the period
    )
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert len(report.downtime) == 1
    assert report.downtime[0].equipment_name == "NEXUS"
    assert report.downtime[0].total_seconds == 3 * 3600  # only the in-period 3h, not the full 5h


def test_build_report_downtime_excludes_non_stopping_faults(db_session):
    """is_stop=False means the component fault never actually stopped
    the machine -- it must not count as downtime at all."""
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    _add_fault_log(
        db_session, eq.id, occurred_at=period_start_utc, cleared_at=period_start_utc + timedelta(hours=1),
        is_stop=False,
    )
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert report.downtime == []


def test_build_report_downtime_merges_overlapping_faults(db_session):
    """Two components failing at overlapping times on the same machine
    must count as one span of downtime, not the sum of both durations
    (that would double-count the overlap)."""
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    # 10:00-10:30 and 10:15-10:45 -- overlapping, union is 10:00-10:45 = 45 min.
    _add_fault_log(db_session, eq.id, occurred_at=period_start_utc, cleared_at=period_start_utc + timedelta(minutes=30))
    _add_fault_log(
        db_session, eq.id,
        occurred_at=period_start_utc + timedelta(minutes=15), cleared_at=period_start_utc + timedelta(minutes=45),
    )
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert len(report.downtime) == 1
    assert report.downtime[0].total_seconds == 45 * 60  # union, not 30+30=60


def test_build_report_active_incident_counts_downtime_so_far(db_session):
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    _add_fault_log(
        db_session, eq.id,
        occurred_at=period_start_utc + timedelta(hours=1),
        cleared_at=None,
    )
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert len(report.downtime) == 1
    assert report.downtime[0].total_seconds > 0  # counted through "now"


def test_build_report_no_data_returns_empty_report(db_session):
    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert report.total_orders == 0
    assert report.total_revenue == Decimal("0.00")
    assert report.total_cost == Decimal("0.00")
    assert report.downtime == []
    assert report.venue_performance == []


# --- build_vendor_report ---

def _add_venue_mapping(db, machine_name, venue_provider):
    db.add(VenueMapping(machine_name=machine_name, venue_provider=venue_provider))


def test_build_vendor_report_scopes_to_venues_machines_only(db_session):
    """The core correctness requirement: a venue with 2 machines gets
    both, but a THIRD machine belonging to a different venue must never
    leak into the total."""
    _add_venue_mapping(db_session, "PNR", "PNR Felicity")
    _add_venue_mapping(db_session, "Navi", "PNR Felicity")
    _add_venue_mapping(db_session, "NEXUS", "Forum Kormangala")
    _add_order_summary(db_session, "2026-08-10", "PNR", n=10)
    _add_order_summary(db_session, "2026-08-10", "Navi", n=5)
    _add_order_summary(db_session, "2026-08-10", "NEXUS", n=100)  # different venue -- must not count
    db_session.flush()

    report = build_vendor_report(db_session, "2026-08-01", "2026-08-31", "PNR Felicity")

    assert report.total_orders == 15
    assert report.venue == "PNR Felicity"


def test_build_vendor_report_has_no_revenue_field_at_all():
    """Belt-and-suspenders check on the dataclass shape itself, not
    just template rendering -- a future template bug can't leak
    revenue if the object never carries it."""
    from dataclasses import fields
    from services.management_report import VendorSalesReport

    field_names = {f.name for f in fields(VendorSalesReport)}
    assert "revenue" not in field_names
    assert "cost" not in field_names
    assert "profit" not in field_names


def test_build_vendor_report_venue_provider_matched_case_insensitively(db_session):
    _add_venue_mapping(db_session, "PNR", "PNR Felicity")
    _add_order_summary(db_session, "2026-08-10", "PNR", n=7)
    db_session.flush()

    report = build_vendor_report(db_session, "2026-08-01", "2026-08-31", "pnr felicity")

    assert report.total_orders == 7


def test_build_vendor_report_monthly_breakdown_spans_ytd_range(db_session):
    """Directly matches the explicit requirement: a YTD-shaped date
    range naturally produces a month-by-month breakdown, since monthly
    is always computed regardless of the day-count involved."""
    _add_venue_mapping(db_session, "PNR", "PNR Felicity")
    _add_order_summary(db_session, "2026-02-15", "PNR", n=3)
    _add_order_summary(db_session, "2026-08-10", "PNR", n=4)
    db_session.flush()

    report = build_vendor_report(db_session, "2026-01-01", "2026-08-31", "PNR Felicity")

    assert len(report.monthly) == 8  # Jan through Aug, zero-filled
    by_month = {m.month: m.orders for m in report.monthly}
    assert by_month["2026-02"] == 3
    assert by_month["2026-08"] == 4
    assert by_month["2026-01"] == 0


def test_build_vendor_report_daily_breakdown_includes_zero_days(db_session):
    _add_venue_mapping(db_session, "PNR", "PNR Felicity")
    _add_order_summary(db_session, "2026-08-10", "PNR", n=3)
    db_session.flush()

    report = build_vendor_report(db_session, "2026-08-09", "2026-08-11", "PNR Felicity")

    assert [(d.date, d.orders) for d in report.daily] == [
        ("2026-08-09", 0), ("2026-08-10", 3), ("2026-08-11", 0),
    ]


def test_build_vendor_report_no_mapped_machines_is_all_zero_not_an_error(db_session):
    report = build_vendor_report(db_session, "2026-08-01", "2026-08-31", "Nonexistent Venue")

    assert report.total_orders == 0
    assert report.venue == "Nonexistent Venue"
