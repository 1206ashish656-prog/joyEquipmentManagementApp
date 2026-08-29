"""
Unit tests for services/recurring_costs.py -- pure DB logic, no HTTP.
Uses the shared db_session fixture (tests/conftest.py, in-memory SQLite).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from db.models import CostEntry, Staff, Venue
from services.recurring_costs import (
    RENT_CATEGORY,
    SALARY_CATEGORY,
    STAFF_SOURCE,
    VENUE_SOURCE,
    generate,
    list_candidates,
)


def _add_venue(db, name, rent="10000.00", active=True) -> Venue:
    v = Venue(name=name, monthly_rent=Decimal(rent) if rent is not None else None, active=active)
    db.add(v)
    db.flush()
    return v


def _add_staff(db, name, salary="20000.00", end_date=None) -> Staff:
    s = Staff(name=name, employment_start_date="2026-01-01", employment_end_date=end_date,
              monthly_salary=Decimal(salary) if salary is not None else None)
    db.add(s)
    db.flush()
    return s


def test_list_candidates_includes_active_venue_and_staff_with_amounts_set(db_session):
    _add_venue(db_session, "PNR Felicity", rent="25000.00")
    _add_staff(db_session, "Priya", salary="30000.00")

    candidates = list_candidates(db_session, "2026-08")

    labels = {(c.source_type, c.label, c.amount) for c in candidates}
    assert (VENUE_SOURCE, "PNR Felicity", Decimal("29500.00")) in labels  # 25000 + 18% GST
    assert (STAFF_SOURCE, "Priya", Decimal("30000.00")) in labels  # no GST on salaries
    assert all(not c.already_generated for c in candidates)


def test_rent_candidate_carries_18_percent_gst(db_session):
    _add_venue(db_session, "PNR Felicity", rent="25000.00")
    candidates = list_candidates(db_session, "2026-08")

    rent = next(c for c in candidates if c.source_type == VENUE_SOURCE)
    assert rent.base_amount == Decimal("25000.00")
    assert rent.amount == Decimal("29500.00")
    assert rent.gst_amount == Decimal("4500.00")


def test_salary_candidate_has_no_gst(db_session):
    _add_staff(db_session, "Priya", salary="30000.00")
    candidates = list_candidates(db_session, "2026-08")

    salary = next(c for c in candidates if c.source_type == STAFF_SOURCE)
    assert salary.base_amount == salary.amount == Decimal("30000.00")
    assert salary.gst_amount == Decimal("0.00")


def test_inactive_venue_excluded(db_session):
    _add_venue(db_session, "Closed Venue", rent="15000.00", active=False)
    candidates = list_candidates(db_session, "2026-08")
    assert candidates == []


def test_venue_without_rent_excluded(db_session):
    _add_venue(db_session, "No Rent Venue", rent=None)
    candidates = list_candidates(db_session, "2026-08")
    assert candidates == []


def test_offboarded_staff_excluded(db_session):
    _add_staff(db_session, "Left Staff", salary="20000.00", end_date="2026-07-01")
    candidates = list_candidates(db_session, "2026-08")
    assert candidates == []


def test_staff_without_salary_excluded(db_session):
    _add_staff(db_session, "No Salary Staff", salary=None)
    candidates = list_candidates(db_session, "2026-08")
    assert candidates == []


def test_generate_creates_one_entry_per_venue_and_staff(db_session):
    _add_venue(db_session, "PNR Felicity", rent="25000.00")
    _add_venue(db_session, "Warehouse Site", rent="18000.00")
    _add_staff(db_session, "Priya", salary="30000.00")

    created = generate(db_session, "2026-08", admin_id=None)

    assert created == 3
    entries = db_session.execute(select(CostEntry)).scalars().all()
    assert len(entries) == 3
    rent_entries = [e for e in entries if e.category == RENT_CATEGORY]
    salary_entries = [e for e in entries if e.category == SALARY_CATEGORY]
    assert len(rent_entries) == 2
    assert len(salary_entries) == 1
    assert salary_entries[0].vendor_name is None  # never applicable for salaries
    assert salary_entries[0].item_name == "Priya"
    assert salary_entries[0].amount == Decimal("30000.00")  # no GST
    assert {e.vendor_name for e in rent_entries} == {"PNR Felicity", "Warehouse Site"}
    assert {e.amount for e in rent_entries} == {Decimal("29500.00"), Decimal("21240.00")}  # +18% GST
    assert all("incl. 18% GST" in e.item_name for e in rent_entries)
    assert all(e.recurring_period == "2026-08" for e in entries)
    assert all(e.date == "2026-08-01" for e in entries)


def test_generate_is_idempotent_for_the_same_period(db_session):
    """Explicit requirement: "one entry per venue and staff" -- clicking
    Generate twice for the same month must never duplicate."""
    _add_venue(db_session, "PNR Felicity", rent="25000.00")

    first = generate(db_session, "2026-08", admin_id=None)
    second = generate(db_session, "2026-08", admin_id=None)

    assert first == 1
    assert second == 0
    entries = db_session.execute(select(CostEntry)).scalars().all()
    assert len(entries) == 1


def test_generate_creates_a_new_entry_for_a_different_period(db_session):
    """Recurring means recurring -- a new month gets its own entry, not
    a permanently-skipped "already generated" state."""
    _add_venue(db_session, "PNR Felicity", rent="25000.00")

    generate(db_session, "2026-08", admin_id=None)
    created_september = generate(db_session, "2026-09", admin_id=None)

    assert created_september == 1
    entries = db_session.execute(select(CostEntry)).scalars().all()
    assert len(entries) == 2
    assert {e.recurring_period for e in entries} == {"2026-08", "2026-09"}


def test_candidate_marked_already_generated_after_generate(db_session):
    venue = _add_venue(db_session, "PNR Felicity", rent="25000.00")
    generate(db_session, "2026-08", admin_id=None)

    candidates = list_candidates(db_session, "2026-08")
    assert len(candidates) == 1
    assert candidates[0].already_generated is True
    assert candidates[0].source_id == venue.id


def test_editing_rent_after_generation_does_not_change_past_entry(db_session):
    """A past period's CostEntry is a real historical record -- editing
    the venue's current rent must never rewrite it."""
    venue = _add_venue(db_session, "PNR Felicity", rent="25000.00")
    generate(db_session, "2026-08", admin_id=None)

    venue.monthly_rent = Decimal("30000.00")
    db_session.flush()

    entry = db_session.execute(select(CostEntry)).scalar_one()
    assert entry.amount == Decimal("29500.00")  # unchanged (25000 + 18% GST at generation time)
