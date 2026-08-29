"""FastAPI tests for the Alert Recipients tab (/alert-recipients) --
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
from db.models import AlertRecipient, Base, Staff, User


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


def _add_recipient(SessionLocal, email, active=True) -> int:
    with SessionLocal() as session:
        r = AlertRecipient(email=email, active=active)
        session.add(r)
        session.commit()
        session.refresh(r)
        return r.id


def _add_staff(SessionLocal, name, email=None, employment_end_date=None) -> int:
    with SessionLocal() as session:
        s = Staff(name=name, email=email, employment_start_date="2026-01-01", employment_end_date=employment_end_date)
        session.add(s)
        session.commit()
        session.refresh(s)
        return s.id


def test_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/alert-recipients", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    assert test_client.get("/alert-recipients").status_code == 403


def test_venue_partner_cannot_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner")
    _login(test_client, "venue@example.com")
    assert test_client.get("/alert-recipients").status_code == 403


def test_admin_can_view(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    resp = test_client.get("/alert-recipients")
    assert resp.status_code == 200
    assert "Alert Recipients" in resp.text


def test_create_recipient(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/alert-recipients",
        data={"email": "Ops-External@Example.com", "name": "External Ops"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        r = session.execute(select(AlertRecipient)).scalar_one()
        assert r.email == "ops-external@example.com"  # normalized to lowercase
        assert r.name == "External Ops"
        assert r.active is True


# --- Quick Add from Staff dropdown (backend/api/alert_recipients.py's
# _staff_with_email) ---

def test_active_staff_with_email_appear_in_quick_add_dropdown(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_staff(SessionLocal, "Priya", email="priya@example.com")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/alert-recipients")
    assert resp.status_code == 200
    assert "priya@example.com" in resp.text
    assert "Priya" in resp.text


def test_staff_without_email_excluded_from_dropdown(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_staff(SessionLocal, "NoEmail")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/alert-recipients")
    assert resp.status_code == 200
    assert "No active staff have an email on file" in resp.text


def test_offboarded_staff_excluded_from_dropdown(client):
    """Confirms staff.py's auto-deactivation and this dropdown's exclusion
    are consistent: a left staff member is never re-offered here."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_staff(SessionLocal, "Left Staff", email="left@example.com", employment_end_date="2026-08-01")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/alert-recipients")
    assert resp.status_code == 200
    assert "left@example.com" not in resp.text


def test_quick_add_from_staff_dropdown_creates_recipient(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_staff(SessionLocal, "Priya", email="priya@example.com")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/alert-recipients", data={"email": "priya@example.com", "name": "Priya"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        r = session.execute(select(AlertRecipient)).scalar_one()
        assert r.email == "priya@example.com"
        assert r.name == "Priya"
        assert r.active is True


def test_create_recipient_rejects_invalid_email(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/alert-recipients", data={"email": "not-an-email"})
    assert resp.status_code == 400
    # Jinja2 autoescapes the apostrophe in "doesn't" to &#39; -- match
    # around it rather than the literal ASCII quote.
    assert "look like a valid email" in resp.text
    with SessionLocal() as session:
        assert session.execute(select(AlertRecipient)).scalars().all() == []


def test_create_recipient_rejects_duplicate(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_recipient(SessionLocal, "ops@example.com")
    _login(test_client, "admin@example.com")

    resp = test_client.post("/alert-recipients", data={"email": "ops@example.com"})
    assert resp.status_code == 400
    assert "already on the alert recipient list" in resp.text


def test_deactivate_and_activate_recipient(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    recipient_id = _add_recipient(SessionLocal, "ops@example.com")
    _login(test_client, "admin@example.com")

    test_client.post(f"/alert-recipients/{recipient_id}/deactivate")
    with SessionLocal() as session:
        assert session.get(AlertRecipient, recipient_id).active is False

    test_client.post(f"/alert-recipients/{recipient_id}/activate")
    with SessionLocal() as session:
        assert session.get(AlertRecipient, recipient_id).active is True


def test_operations_cannot_create_recipient(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")

    resp = test_client.post("/alert-recipients", data={"email": "ops-external@example.com"})
    assert resp.status_code == 403
    with SessionLocal() as session:
        assert session.execute(select(AlertRecipient)).scalars().all() == []
