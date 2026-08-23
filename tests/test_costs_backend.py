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
from db.models import Base, CostEntry, User


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


def _seed_entry(SessionLocal, date, category, vendor_name, item_name, amount):
    with SessionLocal() as session:
        session.add(CostEntry(date=date, category=category, vendor_name=vendor_name, item_name=item_name, amount=Decimal(amount)))
        session.commit()


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
