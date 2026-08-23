"""
FastAPI dashboard tests (Phase 5) via TestClient — no live server, no
Playwright, no real target site. Uses an in-memory SQLite DB (via
db_base.init_engine dependency override), same as test_state_manager.py.

Covers: login/logout, unauthenticated redirect, dashboard counts/table,
equipment detail, active faults, RBAC on /users, and the subscription
create/toggle/delete flow.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, User
from services.health_engine import HealthEngine
from services.state_manager import StateManager
from tests.conftest import make_record


@pytest.fixture()
def client(monkeypatch):
    # StaticPool: FastAPI runs sync route handlers in a worker thread, so
    # without a single shared connection each thread would get its OWN
    # separate ":memory:" database (SQLite's per-connection default) and
    # see no tables.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # Point db.base at this in-memory engine instead of a real Postgres —
    # backend.deps.get_db() goes through db_base.get_session(), so this
    # covers the whole app without any FastAPI dependency_overrides magic.
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)

    # Deliberately NOT using TestClient as a context manager: that would
    # fire the app's startup event, which calls db_base.init_engine() with
    # REAL settings (a Postgres URL that isn't reachable here) and would
    # clobber the SQLite engine just set above. Without `with`, the
    # lifespan never runs, and get_db()/get_session() just use what we
    # monkeypatched.
    test_client = TestClient(app)
    yield test_client, SessionLocal


def _make_admin(SessionLocal, email="admin@example.com", password="correct-horse"):
    with SessionLocal() as session:
        user = User(name="Admin", email=email, role="admin", active=True, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def _login(client, email, password):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_dashboard_redirects_to_login_when_unauthenticated(client):
    test_client, _ = client
    resp = test_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_with_wrong_password_shows_error(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    resp = _login(test_client, "admin@example.com", "wrong-password")
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text


def test_login_with_unknown_email_shows_generic_error(client):
    test_client, _ = client
    resp = _login(test_client, "nobody@example.com", "whatever")
    assert resp.status_code == 401
    assert "Invalid email or password" in resp.text


def test_login_success_sets_cookie_and_redirects(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    resp = _login(test_client, "admin@example.com", "correct-horse")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session" in resp.cookies


def test_dashboard_shows_after_login(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert "Fleet Overview" in resp.text


def test_dashboard_counts_reflect_equipment_health(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")

    with SessionLocal() as session:
        sm = StateManager(HealthEngine())
        sm.process_observation(session, make_record(equipment_id="1", equipment_code="CODE1", name="Healthy One"))
        sm.process_observation(
            session, make_record(equipment_id="2", equipment_code="CODE2", name="Faulty One", fault_type="Motor Fault")
        )
        session.commit()

    resp = test_client.get("/")
    assert resp.status_code == 200
    assert 'class="n">1<' in resp.text.replace("\n", "")  # loose check, see below
    assert "Healthy One" in resp.text
    assert "Faulty One" in resp.text
    assert "🔴" in resp.text


def test_logout_clears_session(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")
    assert test_client.get("/").status_code == 200

    test_client.post("/logout", follow_redirects=False)
    resp = test_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_equipment_detail_page(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")

    with SessionLocal() as session:
        sm = StateManager(HealthEngine())
        result = sm.process_observation(session, make_record(fault_type="Motor Fault"))
        equipment_id = result.equipment.id
        session.commit()

    resp = test_client.get(f"/equipment/{equipment_id}")
    assert resp.status_code == 200
    assert "Motor Fault" in resp.text
    assert "NEXUS" in resp.text  # default name from make_record


def test_equipment_detail_404_for_unknown_id(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")
    resp = test_client.get("/equipment/999999")
    assert resp.status_code == 404


def test_active_faults_page_lists_open_incident(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")

    with SessionLocal() as session:
        sm = StateManager(HealthEngine())
        sm.process_observation(session, make_record(fault_type="Motor Fault"))
        session.commit()

    resp = test_client.get("/faults")
    assert resp.status_code == 200
    assert "Motor Fault" in resp.text
    assert "NEXUS" in resp.text


def test_active_faults_page_empty_state(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")
    resp = test_client.get("/faults")
    assert "No active faults" in resp.text


def test_non_admin_cannot_access_users_page(client):
    test_client, SessionLocal = client
    with SessionLocal() as session:
        session.add(User(name="Regular", email="user@example.com", role="user", active=True, password_hash=hash_password("pw123456")))
        session.commit()
    _login(test_client, "user@example.com", "pw123456")

    resp = test_client.get("/users")
    assert resp.status_code == 403


def test_admin_can_create_user(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")

    resp = test_client.post(
        "/users",
        data={"name": "New Guy", "email": "newguy@example.com", "password": "somepassword", "role": "user"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as session:
        from sqlalchemy import select
        created = session.execute(select(User).where(User.email == "newguy@example.com")).scalar_one()
        assert created.role == "user"
        assert created.password_hash is not None
        assert created.password_hash != "somepassword"  # never stored in plaintext


def test_password_hash_never_rendered_in_users_page(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal, password="admin-secret-pw")
    _login(test_client, "admin@example.com", "admin-secret-pw")
    resp = test_client.get("/users")
    assert "admin-secret-pw" not in resp.text
    assert "pbkdf2_sha256" not in resp.text


def test_subscription_create_toggle_delete_flow(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")

    resp = test_client.post("/subscriptions", data={"equipment_id": "", "severity": "Critical"}, follow_redirects=False)
    assert resp.status_code == 303

    resp = test_client.get("/subscriptions")
    assert "Critical" in resp.text

    with SessionLocal() as session:
        from db.models import AlertSubscription
        sub = session.query(AlertSubscription).one()
        sub_id = sub.id
        assert sub.enabled is True

    test_client.post(f"/subscriptions/{sub_id}/toggle", follow_redirects=False)
    with SessionLocal() as session:
        from db.models import AlertSubscription
        assert session.get(AlertSubscription, sub_id).enabled is False

    test_client.post(f"/subscriptions/{sub_id}/delete", follow_redirects=False)
    with SessionLocal() as session:
        from db.models import AlertSubscription
        assert session.get(AlertSubscription, sub_id) is None


def test_monitoring_status_api_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/api/monitoring/status", follow_redirects=False)
    assert resp.status_code == 303


def test_monitoring_status_api_returns_json(client):
    test_client, SessionLocal = client
    _make_admin(SessionLocal)
    _login(test_client, "admin@example.com", "correct-horse")
    resp = test_client.get("/api/monitoring/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "worker_status" in data
    assert "target_session" in data
