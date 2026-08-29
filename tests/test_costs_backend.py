"""FastAPI tests for the Cost Management tab (/costs) -- admin-only,
same TestClient + StaticPool SQLite pattern as test_orders_backend.py."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, CostEntry, Staff, User, Venue


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    test_client = TestClient(app)
    yield test_client, SessionLocal


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password)))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _seed_entry(SessionLocal, date, category, vendor_name, item_name, amount) -> int:
    with SessionLocal() as session:
        entry = CostEntry(date=date, category=category, vendor_name=vendor_name, item_name=item_name, amount=Decimal(amount))
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry.id


def test_costs_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/costs", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view_costs(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/costs")
    assert resp.status_code == 403


def test_venue_partner_cannot_view_costs(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/costs")
    assert resp.status_code == 403


def test_admin_can_view_costs(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    resp = test_client.get("/costs")
    assert resp.status_code == 200
    assert "Cost Management" in resp.text


def test_costs_nav_link_admin_only(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_user(SessionLocal, "ops@example.com", "operations")

    _login(test_client, "admin@example.com")
    assert "Cost Management" in test_client.get("/").text

    _login(test_client, "ops@example.com")
    assert "Cost Management" not in test_client.get("/").text


def test_create_entry_standard_category(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/costs",
        data={
            "date": "2026-08-24", "category": "Oranges", "vendor_name": "Fresh Farms",
            "item_name": "50kg Valencia oranges", "amount": "5000.00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.category == "Oranges"
        assert entry.vendor_name == "Fresh Farms"
        assert entry.amount == Decimal("5000.00")


def test_create_entry_others_category_uses_custom_text(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post(
        "/costs",
        data={
            "date": "2026-08-24", "category": "Others", "custom_category": "Software Subscription",
            "vendor_name": "Notion", "item_name": "Annual plan", "amount": "1200.00",
        },
        follow_redirects=False,
    )
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.category == "Software Subscription"  # not literally "Others"


def test_create_entry_blank_vendor_gets_placeholder(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post(
        "/costs",
        data={"date": "2026-08-24", "category": "Glass", "vendor_name": "", "item_name": "Glass cups", "amount": "800.00"},
        follow_redirects=False,
    )
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.vendor_name == "UNSPECIFIED"


def test_create_entry_staff_salaries_has_no_vendor(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post(
        "/costs",
        data={
            "date": "2026-08-24", "category": "Staff Salaries", "vendor_name": "should be ignored",
            "item_name": "August salary", "amount": "50000.00",
        },
        follow_redirects=False,
    )
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.vendor_name is None  # never asked/stored for this category


def test_summary_breaks_down_by_category(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Oranges", "5000.00")
    _seed_entry(SessionLocal, "2026-08-24", "Rent", "Landlord", "Rent", "30000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24")
    assert resp.status_code == 200
    assert "Oranges" in resp.text
    assert "Rent" in resp.text
    assert "35000.00" in resp.text  # total tile


def test_summary_custom_period_range(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-01", "Rent", "Landlord", "Rent", "30000.00")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Oranges", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=custom&start=2026-08-01&end=2026-08-10")
    assert resp.status_code == 200
    assert "30000.00" in resp.text
    # The excluded entry's amount shouldn't appear (its category name,
    # "Oranges", is a poor check here -- it's always listed in the
    # add-entry form's dropdown regardless of the summary filter).
    assert "5000.00" not in resp.text


# --- Raw data view (view by selected category/vendor/item) ---

def test_raw_entries_hidden_by_default(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "50kg oranges", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24")
    assert resp.status_code == 200
    assert "Raw Entries" not in resp.text
    # "50kg oranges" still legitimately appears in the item filter
    # dropdown regardless of the raw-data toggle -- the Edit/Delete
    # controls only render inside the raw entries table, so their
    # absence is the precise signal that the table itself is hidden.
    assert ">Edit<" not in resp.text


def test_raw_entries_shown_when_explicitly_requested(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "50kg oranges", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24&show_raw_data=yes")
    assert resp.status_code == 200
    assert "Raw Entries" in resp.text
    assert "50kg oranges" in resp.text
    assert "Fresh Farms" in resp.text


def test_filter_by_category_narrows_raw_entries_and_total(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Oranges", "5000.00")
    _seed_entry(SessionLocal, "2026-08-24", "Rent", "Landlord", "Rent", "30000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24&filter_category=Rent&show_raw_data=yes")
    assert resp.status_code == 200
    assert "30000.00" in resp.text
    assert "5000.00" not in resp.text


def test_filter_by_vendor_narrows_to_that_vendor_only(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Other Farms", "Batch B", "3000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24&filter_vendor=Other+Farms&show_raw_data=yes")
    assert resp.status_code == 200
    assert "3000.00" in resp.text
    assert "5000.00" not in resp.text


def test_filter_by_item_narrows_to_that_item_only(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch B", "3000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24&filter_item=Batch+B&show_raw_data=yes")
    assert resp.status_code == 200
    assert "3000.00" in resp.text
    assert "5000.00" not in resp.text


# --- Edit ---

def test_edit_form_prefilled_for_standard_category(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/costs/{entry_id}/edit")
    assert resp.status_code == 200
    assert 'value="Batch A"' in resp.text
    assert 'value="Fresh Farms"' in resp.text


def test_edit_form_prefilled_for_custom_category(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Marketing", "Insta Ads", "Promo", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/costs/{entry_id}/edit")
    assert resp.status_code == 200
    assert 'value="Marketing"' in resp.text  # pre-filled into the custom_category field
    assert 'value="Others"' in resp.text  # category select defaults to Others


def test_edit_updates_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/costs/{entry_id}/edit",
        data={
            "date": "2026-08-25", "category": "Rent", "vendor_name": "Landlord Co.",
            "item_name": "Corrected entry", "amount": "6000.00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        entry = session.get(CostEntry, entry_id)
        assert entry.date == "2026-08-25"
        assert entry.category == "Rent"
        assert entry.amount == Decimal("6000.00")
        assert entry.updated_at is not None


def test_edit_to_others_uses_custom_category_text(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/costs/{entry_id}/edit",
        data={
            "date": "2026-08-24", "category": "Others", "custom_category": "Repairs",
            "vendor_name": "Fix-It Co.", "item_name": "AC repair", "amount": "2000.00",
        },
    )
    with SessionLocal() as session:
        entry = session.get(CostEntry, entry_id)
        assert entry.category == "Repairs"


def test_edit_to_staff_salaries_clears_vendor(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/costs/{entry_id}/edit",
        data={"date": "2026-08-24", "category": "Staff Salaries", "item_name": "August salary", "amount": "50000.00"},
    )
    with SessionLocal() as session:
        entry = session.get(CostEntry, entry_id)
        assert entry.vendor_name is None


def test_operations_cannot_edit_cost_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "ops@example.com")
    assert test_client.get(f"/costs/{entry_id}/edit").status_code == 403


# --- Delete ---

def test_delete_removes_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.post(f"/costs/{entry_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(CostEntry, entry_id) is None


def test_operations_cannot_delete_cost_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "ops@example.com")

    resp = test_client.post(f"/costs/{entry_id}/delete")
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.get(CostEntry, entry_id) is not None  # not deleted


def test_delete_nonexistent_entry_is_a_no_op_not_an_error(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/costs/999999/delete", follow_redirects=False)
    assert resp.status_code == 303


# --- Item / description is optional (2026-08-28) ---

def test_create_entry_without_item_name_stores_empty_string(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/costs",
        data={"date": "2026-08-28", "category": "Oranges", "vendor_name": "Fresh Farms", "amount": "500.00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry).where(CostEntry.date == "2026-08-28")).scalar_one()
        assert entry.item_name == ""


def test_create_entry_with_blank_or_whitespace_item_name_stores_empty_string(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    test_client.post(
        "/costs",
        data={"date": "2026-08-28", "category": "Oranges", "vendor_name": "Fresh Farms", "item_name": "   ", "amount": "500.00"},
        follow_redirects=False,
    )
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry).where(CostEntry.date == "2026-08-28")).scalar_one()
        assert entry.item_name == ""


def test_raw_entries_show_placeholder_for_blank_item_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?period=daily&as_of=2026-08-24&show_raw_data=yes")
    assert "—" in resp.text


def test_edit_entry_can_clear_item_name(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    entry_id = _seed_entry(SessionLocal, "2026-08-24", "Oranges", "Fresh Farms", "Batch A", "5000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/costs/{entry_id}/edit",
        data={"date": "2026-08-24", "category": "Oranges", "vendor_name": "Fresh Farms", "item_name": "", "amount": "5000.00"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(CostEntry, entry_id).item_name == ""


# --- Recurring Costs (services/recurring_costs.py) ---

def _add_venue(SessionLocal, name, rent="25000.00"):
    with SessionLocal() as session:
        session.add(Venue(name=name, monthly_rent=Decimal(rent), active=True))
        session.commit()


def _add_staff_with_salary(SessionLocal, name, salary="30000.00"):
    with SessionLocal() as session:
        session.add(Staff(name=name, employment_start_date="2026-01-01", monthly_salary=Decimal(salary)))
        session.commit()


def test_costs_page_shows_pending_recurring_candidates(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity")
    _add_staff_with_salary(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/costs?recurring_period=2026-08")
    assert resp.status_code == 200
    assert "PNR Felicity" in resp.text
    assert "Priya" in resp.text
    assert "Generate 2 entries for 2026-08" in resp.text


def test_generate_recurring_costs_creates_entries(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity", rent="25000.00")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/costs/recurring/generate", data={"period": "2026-08"}, follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.category == "Rent"
        assert entry.vendor_name == "PNR Felicity"
        assert entry.amount == Decimal("25000.00")
        assert entry.recurring_period == "2026-08"


def test_generate_recurring_costs_twice_does_not_duplicate(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity")
    _login(test_client, "admin@example.com")

    test_client.post("/costs/recurring/generate", data={"period": "2026-08"})
    test_client.post("/costs/recurring/generate", data={"period": "2026-08"})

    with SessionLocal() as session:
        assert len(session.execute(select(CostEntry)).scalars().all()) == 1


def test_generate_recurring_costs_appears_in_raw_entries_and_rollup(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity", rent="25000.00")
    _login(test_client, "admin@example.com")

    test_client.post("/costs/recurring/generate", data={"period": "2026-08"})

    resp = test_client.get("/costs?period=monthly&as_of=2026-08-15&show_raw_data=yes")
    assert resp.status_code == 200
    assert "PNR Felicity" in resp.text
    assert "25000.00" in resp.text


def test_operations_cannot_generate_recurring_costs(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _add_venue(SessionLocal, "PNR Felicity")
    _login(test_client, "ops@example.com")

    resp = test_client.post("/costs/recurring/generate", data={"period": "2026-08"})
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.execute(select(CostEntry)).scalars().all() == []


def test_generate_recurring_costs_falls_back_to_current_month_on_bad_period(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "PNR Felicity")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/costs/recurring/generate", data={"period": "not-a-period"}, follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as session:
        entry = session.execute(select(CostEntry)).scalar_one()
        assert entry.recurring_period is not None
        assert len(entry.recurring_period) == 7  # a real 'YYYY-MM', not the malformed input
