"""FastAPI tests for the Senior Management Report (/reports/management)
-- admin-only, same TestClient + StaticPool SQLite pattern as
test_costs_backend.py. The PDF route's real Playwright rendering
(services/report_pdf.render_html_to_pdf) is monkeypatched here -- a
real headless-Chromium launch is exercised separately via live
verification against the running app, not in this fast unit suite
(same convention as every other Playwright-touching path in this
project: monitoring/*'s automated tests all mock the HTTP layer rather
than spinning a real browser)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
import services.report_pdf as report_pdf
from backend.main import app
from backend.security import hash_password
from db.models import Base, Equipment, OrderSummary, User


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)

    async def fake_render(html: str) -> bytes:
        return b"%PDF-1.4 fake pdf bytes"

    monkeypatch.setattr(report_pdf, "render_html_to_pdf", fake_render)

    test_client = TestClient(app)
    yield test_client, SessionLocal


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password)))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_equipment(SessionLocal, name, external_id) -> int:
    with SessionLocal() as session:
        eq = Equipment(external_id=external_id, equipment_code=f"CODE-{external_id}", name=name)
        session.add(eq)
        session.commit()
        session.refresh(eq)
        return eq.id


def _add_order(SessionLocal, date, device_app, n=10):
    with SessionLocal() as session:
        session.add(OrderSummary(
            date=date, device_app=device_app, price=Decimal("120.00"), pay_type="UPI",
            number_of_orders=n, total_number_of_oranges=25, average_juice_weight=Decimal("200.00"),
        ))
        session.commit()


def test_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/reports/management", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/reports/management")
    assert resp.status_code == 403


def test_venue_partner_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "vp@example.com", "venue_partner")
    _login(test_client, "vp@example.com")
    resp = test_client.get("/reports/management")
    assert resp.status_code == 403


def test_admin_can_view_report_page(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_equipment(SessionLocal, "NEXUS", "205")
    _add_order(SessionLocal, "2026-08-10", "NEXUS", n=10)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management?period=monthly&as_of=2026-08-15")
    assert resp.status_code == 200
    assert "Senior Management Report" in resp.text
    assert "All Machines" in resp.text
    assert "10" in resp.text  # order count somewhere in the tiles/table


def test_report_scoped_to_one_machine_shows_no_cost(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    eq_id = _add_equipment(SessionLocal, "NEXUS", "205")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/reports/management?period=monthly&as_of=2026-08-15&equipment_id={eq_id}")
    assert resp.status_code == 200
    assert "NEXUS" in resp.text
    assert "only meaningful company-wide" in resp.text


def test_operations_cannot_download_pdf(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/reports/management/pdf")
    assert resp.status_code == 403


def test_admin_can_download_pdf(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management/pdf?period=monthly&as_of=2026-08-15")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content == b"%PDF-1.4 fake pdf bytes"


def test_pdf_filename_reflects_period_and_scope(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    eq_id = _add_equipment(SessionLocal, "NEXUS", "205")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/reports/management/pdf?period=monthly&as_of=2026-08-15&equipment_id={eq_id}")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "2026-08-01" in disposition
    assert "2026-08-31" in disposition
    assert "NEXUS" in disposition


def test_invalid_equipment_id_falls_back_to_all_machines(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/reports/management?equipment_id=not-a-number")
    assert resp.status_code == 200
    assert "All Machines" in resp.text
