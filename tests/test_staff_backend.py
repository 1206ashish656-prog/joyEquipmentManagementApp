"""FastAPI tests for the Staff & Leave Management tab (/staff) --
admin-only, same TestClient + StaticPool SQLite pattern as
test_costs_backend.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, Staff, StaffLeave, User


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


def _add_staff(SessionLocal, name, start="2026-01-01", end=None) -> int:
    with SessionLocal() as session:
        s = Staff(name=name, employment_start_date=start, employment_end_date=end)
        session.add(s)
        session.commit()
        session.refresh(s)
        return s.id


def test_staff_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/staff", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    assert test_client.get("/staff").status_code == 403


def test_venue_partner_cannot_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner")
    _login(test_client, "venue@example.com")
    assert test_client.get("/staff").status_code == 403


def test_admin_can_view_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    resp = test_client.get("/staff")
    assert resp.status_code == 200
    assert "Staff" in resp.text


def test_create_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff",
        data={"name": "Priya", "employment_start_date": "2026-06-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        staff = session.execute(select(Staff)).scalar_one()
        assert staff.name == "Priya"
        assert staff.employment_end_date is None


def test_offboard_staff_sets_end_date(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post(f"/staff/{staff_id}/offboard", data={"end_date": "2026-08-24"}, follow_redirects=False)
    with SessionLocal() as session:
        staff = session.get(Staff, staff_id)
        assert staff.employment_end_date == "2026-08-24"


def test_log_leave_for_staff(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/staff/leaves",
        data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-13", "reason": "Family event"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        leave = session.execute(select(StaffLeave)).scalar_one()
        assert leave.staff_id == staff_id
        assert leave.reason == "Family event"


def test_leave_summary_highlights_staff_over_threshold(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post("/staff/leaves", data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-13"})

    resp = test_client.get("/staff?year=2026&month=8")
    assert resp.status_code == 200
    assert "Exceeds 2 days" in resp.text


def test_leave_summary_does_not_highlight_short_leave(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    staff_id = _add_staff(SessionLocal, "Priya")
    _login(test_client, "admin@example.com")

    test_client.post("/staff/leaves", data={"staff_id": staff_id, "start_date": "2026-08-10", "end_date": "2026-08-11"})

    resp = test_client.get("/staff?year=2026&month=8")
    assert resp.status_code == 200
    assert "Exceeds 2 days" not in resp.text
