"""
Unit tests for services/management_report.py -- pure DB logic, no HTTP.
Uses the shared db_session fixture (tests/conftest.py, in-memory SQLite).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from db.models import CostEntry, Equipment, FaultIncident, HealthState, IncidentStatus, OrderSummary, VenueMapping
from orders.mapping import IST_TZ
from services.management_report import (
    _months_between,
    _time_of_day_seconds,
    build_report,
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


def _add_incident(db, equipment_id, started_at, resolved_at=None, status=IncidentStatus.RESOLVED):
    db.add(FaultIncident(
        equipment_id=equipment_id, fault_type="Simulated", severity="Critical",
        current_health=HealthState.OFFLINE, started_at=started_at, resolved_at=resolved_at, status=status,
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
    """An incident starting before the period and resolved partway
    through must only count the overlapping portion -- same clipping
    idiom as staff/leave_summary.py's month-boundary handling."""
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    _add_incident(
        db_session, eq.id,
        started_at=period_start_utc - timedelta(hours=2),  # started before the period
        resolved_at=period_start_utc + timedelta(hours=3),  # resolved 3h into the period
    )
    db_session.flush()

    report = build_report(db_session, "2026-08-01", "2026-08-31", equipment_id=None)

    assert len(report.downtime) == 1
    assert report.downtime[0].equipment_name == "NEXUS"
    assert report.downtime[0].total_seconds == 3 * 3600  # only the in-period 3h, not the full 5h


def test_build_report_active_incident_counts_downtime_so_far(db_session):
    eq = _add_equipment(db_session, "NEXUS", "205")
    period_start_utc = datetime(2026, 8, 1, tzinfo=IST_TZ).astimezone(timezone.utc)
    _add_incident(
        db_session, eq.id,
        started_at=period_start_utc + timedelta(hours=1),
        resolved_at=None, status=IncidentStatus.ACTIVE,
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
