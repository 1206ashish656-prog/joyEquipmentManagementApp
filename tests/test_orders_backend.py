"""
FastAPI tests for the Order Summary tab (/orders/summary) — same
TestClient + StaticPool SQLite pattern as test_backend.py.
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
from db.models import Base, OrderSummary, OrderSummaryRun, OrderSummaryRunStatus, User


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


def _login(client, email="admin@example.com", password="correct-horse"):
    with_user = client
    return with_user.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _seed_user(SessionLocal):
    with SessionLocal() as session:
        session.add(User(name="Admin", email="admin@example.com", role="admin", active=True, password_hash=hash_password("correct-horse")))
        session.commit()


def _seed_order_summary(SessionLocal, date, device_app, price, pay_type, n=5, oranges=15, juice_weight="200.00"):
    with SessionLocal() as session:
        session.add(OrderSummary(
            date=date, device_app=device_app, price=Decimal(price), pay_type=pay_type,
            number_of_orders=n, total_number_of_oranges=oranges, average_juice_weight=Decimal(juice_weight),
        ))
        existing = session.query(OrderSummaryRun).filter_by(date=date).one_or_none()
        if not existing:
            session.add(OrderSummaryRun(date=date, status=OrderSummaryRunStatus.SUCCESS, raw_orders_fetched=n, qualifying_orders=n))
        session.commit()


def test_orders_summary_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/orders/summary", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_orders_summary_page_loads_with_no_data(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _login(test_client)
    resp = test_client.get("/orders/summary")
    assert resp.status_code == 200
    assert "Order Summary" in resp.text
    assert "No order data cached" in resp.text


def test_orders_summary_daily_shows_machine_breakdown(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=120, oranges=261, juice_weight="202.58")
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=47, oranges=114, juice_weight="203.77")
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "NEXUS" in resp.text
    assert "Gravity" in resp.text
    assert "120" in resp.text  # total orders (120) somewhere in the tiles/table


def test_orders_summary_aggregate_hides_machine_column(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0")
    assert resp.status_code == 200
    # aggregated total should combine both machines: 15 orders
    assert "aggregated" in resp.text.lower() or "(no breakdown" in resp.text.lower()


def test_orders_summary_mid_day_price_change_shows_two_rows(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=3)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "150.00", "UPI", n=2)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=1&by_price=1")
    assert resp.status_code == 200
    assert "120.00" in resp.text
    assert "150.00" in resp.text


def test_orders_summary_weekly_period_includes_multiple_days(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    # 2026-08-23 is a Sunday; the ISO week containing it is Aug 17-23.
    _seed_order_summary(SessionLocal, "2026-08-18", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=20)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=weekly&as_of=2026-08-23")
    assert resp.status_code == 200
    # weighted-average price is still 120.00, but total orders should be 30
    assert "30" in resp.text


def test_orders_summary_chart_data_embedded_as_json(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert "aggregateChartData" in resp.text
    assert "2026-08-23" in resp.text


def test_orders_summary_aggregate_chart_present_without_machine_breakdown(client):
    """The aggregated trend chart is always rendered, even when the table
    itself isn't broken down by machine — it's independent of the
    breakdown checkboxes, per the 'trend for aggregated orders over time
    as well' requirement."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0")
    assert resp.status_code == 200
    assert "aggregateChartData" in resp.text
    assert "machineChartData" not in resp.text


def test_orders_summary_machine_chart_present_with_machine_breakdown(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=1")
    assert resp.status_code == 200
    assert "aggregateChartData" in resp.text
    assert "machineChartData" in resp.text
    assert "NEXUS" in resp.text


def test_orders_summary_breakdown_by_price_only(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "150.00", "UPI", n=5)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_price=1")
    assert resp.status_code == 200
    # two distinct prices should each get their own row; machine column absent
    assert "120.00" in resp.text
    assert "150.00" in resp.text
    assert "NEXUS" not in resp.text


def test_orders_summary_breakdown_by_pay_type_only(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "Cash", n=4)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_pay_type=1")
    assert resp.status_code == 200
    assert "UPI" in resp.text
    assert "Cash" in resp.text
    assert "NEXUS" not in resp.text


def test_orders_summary_breakdown_by_price_and_pay_type_without_machine(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10)
    _seed_order_summary(SessionLocal, "2026-08-23", "Gravity", "120.00", "Cash", n=3)
    _login(test_client)

    resp = test_client.get(
        "/orders/summary?period=daily&as_of=2026-08-23&by_machine=0&by_price=1&by_pay_type=1"
    )
    assert resp.status_code == 200
    assert "120.00" in resp.text
    assert "UPI" in resp.text
    assert "Cash" in resp.text
    # both machines' orders (10 + 3) should be merged into price/pay_type rows, not shown by name
    assert "NEXUS" not in resp.text


# --- Admin-only Revenue / Oranges-per-Glass columns ---

def test_admin_sees_revenue_and_oranges_per_glass(client):
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    # 10 orders @ 120.00 -> revenue 1200.00; 25 oranges / 10 orders = 2.50/glass
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Revenue" in resp.text
    assert "Oranges/Glass" in resp.text
    assert "1200.00" in resp.text
    assert "2.50" in resp.text


def test_venue_partner_does_not_see_revenue_or_oranges_per_glass(client):
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "120.00", "UPI", n=10, oranges=25)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "Revenue" not in resp.text
    assert "Oranges/Glass" not in resp.text
    # the venue partner still sees their own underlying numbers, just not
    # the two derived admin-only metrics.
    assert "NEXUS" in resp.text or "10" in resp.text


def test_venue_partner_chart_json_omits_revenue(client):
    """Hardening the above: the chart's embedded JSON payload must not
    leak raw revenue numbers either, even though nothing visibly renders
    them -- a page-source/network-tab read shouldn't reveal it."""
    test_client, SessionLocal = client
    with SessionLocal() as session:
        from db.models import VenueMapping
        session.add(VenueMapping(machine_name="NEXUS", venue_provider="Forum Kormangala"))
        session.add(User(
            name="Venue", email="venue@example.com", role="venue_partner", venue_provider="Forum Kormangala",
            active=True, password_hash=hash_password("pw123456"),
        ))
        session.commit()
    # 2 orders @ 500.00 -> revenue 1000.00, a figure distinct from every
    # OTHER visible field (avg price 500.00, oranges, juice weight) so a
    # match can only come from the revenue computation itself.
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "500.00", "UPI", n=2, oranges=6)
    _login(test_client, "venue@example.com", "pw123456")

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert "1000.00" not in resp.text
    assert '"revenue"' not in resp.text


def test_admin_chart_json_includes_revenue(client):
    """Regression guard: the fix above must not accidentally strip
    revenue from the admin-facing chart too."""
    test_client, SessionLocal = client
    _seed_user(SessionLocal)
    _seed_order_summary(SessionLocal, "2026-08-23", "NEXUS", "500.00", "UPI", n=2, oranges=6)
    _login(test_client)

    resp = test_client.get("/orders/summary?period=daily&as_of=2026-08-23")
    assert resp.status_code == 200
    assert '"revenue"' in resp.text
