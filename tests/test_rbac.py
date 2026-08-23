"""
Role-based access control: admin (full access), operations (equipment
monitoring only), venue_partner (order summary only, scoped to their
own venue's machine(s) via VenueMapping). Same TestClient + StaticPool
SQLite pattern as test_backend.py/test_orders_backend.py.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus, User, VenueMapping


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


def _add_user(SessionLocal, email, role, venue_provider=None, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(
            name=email.split("@")[0], email=email, role=role, venue_provider=venue_provider,
            active=True, password_hash=hash_password(password),
        ))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _seed_venue_mapping(SessionLocal):
    with SessionLocal() as session:
        session.add_all([
            VenueMapping(machine_name="PNR", venue_provider="PNR Felicity"),
            VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"),
            VenueMapping(machine_name="Gravity", venue_provider="Prestige Tech Park"),
        ])
        session.commit()


def _seed_order_summary(SessionLocal, date, device_app, n=10):
    with SessionLocal() as session:
        session.add(OrderSummary(
            date=date, device_app=device_app, price=Decimal("120.00"), pay_type="UPI",
            number_of_orders=n, total_number_of_oranges=n * 2, average_juice_weight=Decimal("200.00"),
        ))
        if not session.query(OrderSummaryRun).filter_by(date=date).count():
            session.add(OrderSummaryRun(date=date, status=OrderSummaryRunStatus.SUCCESS, raw_orders_fetched=n, qualifying_orders=n))
        session.commit()


# --- Login lands each role on its own appropriate home page ---

def test_admin_login_redirects_to_dashboard(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    resp = _login(test_client, "admin@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_operations_login_redirects_to_dashboard(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    resp = _login(test_client, "ops@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_venue_partner_login_redirects_to_order_summary(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    resp = _login(test_client, "venue@example.com")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/orders/summary"


# --- Operations: dashboard/faults/alerts yes, orders/users no ---

def test_operations_can_view_dashboard(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/")
    assert resp.status_code == 200


def test_operations_can_view_faults_and_subscriptions(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    assert test_client.get("/faults").status_code == 200
    assert test_client.get("/subscriptions").status_code == 200


def test_operations_cannot_view_order_summary(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/orders/summary")
    assert resp.status_code == 403


def test_operations_cannot_view_users_page(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/users")
    assert resp.status_code == 403


def test_operations_can_view_monitoring_status_api(client):
    # Part of the same equipment-monitoring area as the dashboard itself.
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/api/monitoring/status")
    assert resp.status_code == 200


def test_venue_partner_cannot_view_monitoring_status_api(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/api/monitoring/status")
    assert resp.status_code == 403


# --- Venue partner: orders yes (scoped), dashboard/faults/users no ---

def test_venue_partner_cannot_view_dashboard(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/")
    assert resp.status_code == 403


def test_venue_partner_cannot_view_faults_or_subscriptions(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _login(test_client, "venue@example.com")
    assert test_client.get("/faults").status_code == 403
    assert test_client.get("/subscriptions").status_code == 403


def test_venue_partner_cannot_view_users_page(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/users")
    assert resp.status_code == 403


def test_venue_partner_sees_only_their_venues_machine(client):
    test_client, SessionLocal = client
    _seed_venue_mapping(SessionLocal)
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _seed_order_summary(SessionLocal, "2026-08-23", "PNR", n=42)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", n=99)  # a different venue's data
    _login(test_client, "venue@example.com")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "42" in resp.text
    assert "99" not in resp.text
    assert "PNR Felicity" in resp.text  # venue shown in the status panel


def test_venue_partner_without_venue_assigned_sees_explanatory_message(client):
    test_client, SessionLocal = client
    _seed_venue_mapping(SessionLocal)
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider=None)
    _seed_order_summary(SessionLocal, "2026-08-23", "PNR", n=42)
    _login(test_client, "venue@example.com")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "isn't assigned to a venue" in resp.text
    assert "42" not in resp.text


# --- Admin: unrestricted, sees all venues' data at once ---

def test_admin_sees_all_venues_order_data(client):
    test_client, SessionLocal = client
    _seed_venue_mapping(SessionLocal)
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_order_summary(SessionLocal, "2026-08-23", "PNR", n=42)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", n=99)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "42" in resp.text
    assert "99" in resp.text


def test_admin_can_access_every_gated_route(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    for path in ("/", "/faults", "/subscriptions", "/orders/summary", "/users", "/api/monitoring/status"):
        resp = test_client.get(path)
        assert resp.status_code == 200, f"admin should reach {path}, got {resp.status_code}"


# --- Nav visibility follows role ---

def test_operations_nav_hides_order_summary_link(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/")
    assert "Order Summary" not in resp.text
    assert "Dashboard" in resp.text


def test_venue_partner_nav_hides_dashboard_link(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner", venue_provider="PNR Felicity")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/orders/summary")
    assert "Order Summary" in resp.text
    assert ">Dashboard<" not in resp.text
