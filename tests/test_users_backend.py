"""FastAPI tests for /users -- admin-only user management, plus the
super-admin gate on the admin role itself (User.is_super_admin). Same
TestClient + StaticPool SQLite pattern as test_staff_backend.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, User, Venue, VenueMapping


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


def _add_user(SessionLocal, email, role, password="pw123456", is_super_admin=False) -> int:
    with SessionLocal() as session:
        u = User(
            name=email.split("@")[0], email=email, role=role, active=True,
            password_hash=hash_password(password), is_super_admin=is_super_admin,
        )
        session.add(u)
        session.commit()
        session.refresh(u)
        return u.id


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_venue(SessionLocal, name, active=True):
    with SessionLocal() as session:
        session.add(Venue(name=name, active=active))
        session.commit()


def _add_venue_mapping(SessionLocal, machine_name, venue_provider):
    with SessionLocal() as session:
        session.add(VenueMapping(machine_name=machine_name, venue_provider=venue_provider))
        session.commit()


# --- RBAC baseline ---

def test_users_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/users", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- Venue dropdown (_venues()) ---

def test_venue_added_at_venues_page_appears_in_add_user_dropdown(client):
    """The actual bug report this fixes: a real venue existed (added at
    /venues) with no machine mapped to it yet, and never showed up here
    at all."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "Brand New Venue")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/users")
    assert resp.status_code == 200
    assert "Brand New Venue" in resp.text


def test_legacy_venue_mapping_only_venue_still_appears(client):
    """Backward compatibility: a venue that only exists via VenueMapping
    (no matching Venue master-list row) must not disappear."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue_mapping(SessionLocal, "NEXUS", "Forum Kormangala")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/users")
    assert resp.status_code == 200
    assert "Forum Kormangala" in resp.text


def test_inactive_venue_does_not_appear_in_dropdown(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_venue(SessionLocal, "Deactivated Venue", active=False)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/users")
    assert resp.status_code == 200
    assert "Deactivated Venue" not in resp.text


def test_venue_dropdown_also_shown_on_edit_user_page(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    target_id = _add_user(SessionLocal, "ops@example.com", "operations")
    _add_venue(SessionLocal, "Brand New Venue")
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/users/{target_id}/edit")
    assert resp.status_code == 200
    assert "Brand New Venue" in resp.text


def test_operations_cannot_view_users(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    assert test_client.get("/users").status_code == 403


# --- Regular admin: full edit rights on non-admin-role fields ---

def test_regular_admin_can_edit_non_admin_users_other_fields(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    target_id = _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/users/{target_id}/edit",
        data={"name": "Renamed", "email": "ops2@example.com", "role": "operations", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        target = session.get(User, target_id)
        assert target.name == "Renamed"
        assert target.email == "ops2@example.com"


def test_regular_admin_can_change_role_between_operations_and_venue_partner(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    target_id = _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/users/{target_id}/edit",
        data={"name": "Ops", "email": "ops@example.com", "role": "venue_partner", "venue_provider": "Forum Kormangala", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(User, target_id).role == "venue_partner"


def test_password_left_blank_keeps_current_password(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    target_id = _add_user(SessionLocal, "ops@example.com", "operations", password="original123")
    _login(test_client, "admin@example.com")

    test_client.post(
        f"/users/{target_id}/edit",
        data={"name": "Ops", "email": "ops@example.com", "role": "operations", "active": "on", "password": ""},
        follow_redirects=False,
    )
    # Original password still works -- login as that user succeeds.
    resp = _login(test_client, "ops@example.com", "original123")
    assert resp.status_code == 303
    assert resp.headers["location"] != "/login"


# --- The admin-role gate itself ---

def test_regular_admin_cannot_create_admin_user(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=False)
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        "/users",
        data={"name": "New Admin", "email": "newadmin@example.com", "password": "pw123456", "role": "admin"},
    )
    assert resp.status_code == 400
    assert "Only a super admin" in resp.text
    with SessionLocal() as session:
        assert session.execute(select(User).where(User.email == "newadmin@example.com")).scalar_one_or_none() is None


def test_regular_admin_cannot_promote_user_to_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=False)
    target_id = _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/users/{target_id}/edit",
        data={"name": "Ops", "email": "ops@example.com", "role": "admin", "active": "on"},
    )
    assert resp.status_code == 400
    assert "Only a super admin" in resp.text
    with SessionLocal() as session:
        assert session.get(User, target_id).role == "operations"


def test_regular_admin_cannot_demote_existing_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=False)
    other_admin_id = _add_user(SessionLocal, "other@example.com", "admin", is_super_admin=False)
    _login(test_client, "admin@example.com")

    resp = test_client.post(
        f"/users/{other_admin_id}/edit",
        data={"name": "Other", "email": "other@example.com", "role": "operations", "active": "on"},
    )
    assert resp.status_code == 400
    with SessionLocal() as session:
        assert session.get(User, other_admin_id).role == "admin"


def test_super_admin_can_promote_user_to_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "super@example.com", "admin", is_super_admin=True)
    target_id = _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "super@example.com")

    resp = test_client.post(
        f"/users/{target_id}/edit",
        data={"name": "Ops", "email": "ops@example.com", "role": "admin", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(User, target_id).role == "admin"


def test_super_admin_can_demote_admin_to_operations(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "super@example.com", "admin", is_super_admin=True)
    other_admin_id = _add_user(SessionLocal, "other@example.com", "admin", is_super_admin=False)
    _login(test_client, "super@example.com")

    resp = test_client.post(
        f"/users/{other_admin_id}/edit",
        data={"name": "Other", "email": "other@example.com", "role": "operations", "active": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.get(User, other_admin_id).role == "operations"


def test_super_admin_can_create_admin_user(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "super@example.com", "admin", is_super_admin=True)
    _login(test_client, "super@example.com")

    resp = test_client.post(
        "/users",
        data={"name": "New Admin", "email": "newadmin@example.com", "password": "pw123456", "role": "admin"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with SessionLocal() as session:
        assert session.execute(select(User).where(User.email == "newadmin@example.com")).scalar_one().role == "admin"


# --- Role <select> visibility ---

def test_admin_option_hidden_for_regular_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=False)
    _login(test_client, "admin@example.com")
    resp = test_client.get("/users")
    assert 'value="admin"' not in resp.text


def test_admin_option_shown_for_super_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "super@example.com", "admin", is_super_admin=True)
    _login(test_client, "super@example.com")
    resp = test_client.get("/users")
    assert 'value="admin"' in resp.text


def test_edit_form_locks_role_for_admin_target_viewed_by_regular_admin(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=False)
    other_admin_id = _add_user(SessionLocal, "other@example.com", "admin", is_super_admin=False)
    _login(test_client, "admin@example.com")

    resp = test_client.get(f"/users/{other_admin_id}/edit")
    assert resp.status_code == 200
    assert "locked" in resp.text.lower()


# --- Super admin badge ---

def test_super_admin_badge_shown_only_for_super_admin_row(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "super@example.com", "admin", is_super_admin=True)
    _add_user(SessionLocal, "other@example.com", "admin", is_super_admin=False)
    _login(test_client, "super@example.com")

    resp = test_client.get("/users")
    assert resp.text.count("Super Admin") == 1


# --- Edge cases ---

def test_edit_nonexistent_user_redirects_without_error(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin", is_super_admin=True)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/users/99999/edit", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/users"
