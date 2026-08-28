"""FastAPI tests for Inventory Management (/inventory) -- admin OR
operations (not venue_partner), same TestClient + StaticPool SQLite
pattern as test_costs_backend.py. SMTP is forced to the console-fallback
channel by default (smtp_host="") so no test ever attempts a real
network connection, matching test_backend.py's "no live anything"
philosophy -- the one test that needs to verify a real send mocks
smtplib.SMTP directly, same as test_notification_service.py."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.deps as backend_deps
import db.base as db_base
from backend.main import app
from backend.security import hash_password
from db.models import Base, InventoryItem, InventoryLogEntry, User
from monitoring.config import load_settings


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)

    # Force console-fallback email (no real SMTP) unless a specific test
    # overrides this itself.
    monkeypatch.setattr(backend_deps, "load_settings", lambda: replace(load_settings(), smtp_host=""))

    test_client = TestClient(app)
    yield test_client, SessionLocal


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password)))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _seed_item(SessionLocal, key="dustbin_bags", name="Dustbin Bags", unit="packs", current_stock=10, pieces_per_carton=None) -> int:
    with SessionLocal() as session:
        item = InventoryItem(key=key, name=name, unit=unit, current_stock=current_stock, pieces_per_carton=pieces_per_carton)
        session.add(item)
        session.commit()
        session.refresh(item)
        return item.id


# --- RBAC ---

def test_inventory_requires_login(client):
    test_client, _ = client
    resp = test_client.get("/inventory", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_venue_partner_cannot_view_inventory(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "venue@example.com", "venue_partner")
    _login(test_client, "venue@example.com")
    resp = test_client.get("/inventory")
    assert resp.status_code == 403


def test_operations_can_view_inventory(client):
    """Key difference from Cost Management: operations IS allowed here."""
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/inventory")
    assert resp.status_code == 200
    assert "Inventory Management" in resp.text


def test_admin_can_view_inventory(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    resp = test_client.get("/inventory")
    assert resp.status_code == 200


def test_inventory_nav_link_visible_for_admin_and_operations_not_venue_partner(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_user(SessionLocal, "ops@example.com", "operations")
    _add_user(SessionLocal, "venue@example.com", "venue_partner")

    _login(test_client, "admin@example.com")
    assert "Inventory" in test_client.get("/").text

    _login(test_client, "ops@example.com")
    assert "Inventory" in test_client.get("/").text

    _login(test_client, "venue@example.com")
    assert "Inventory" not in test_client.get("/orders/summary").text


# --- Log usage ---

def test_log_usage_decrements_stock_and_creates_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    item_id = _seed_item(SessionLocal, current_stock=10)
    _login(test_client, "ops@example.com")

    resp = test_client.post(
        "/inventory/log-usage",
        data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 3, "note": "daily usage"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as session:
        item = session.get(InventoryItem, item_id)
        assert item.current_stock == 7
        entry = session.execute(select(InventoryLogEntry).where(InventoryLogEntry.item_id == item_id)).scalar_one()
        assert entry.entry_type == "usage"
        assert entry.quantity_change == -3
        assert entry.resulting_stock == 7
        assert entry.note == "daily usage"
        assert entry.created_by_user_id is not None


def test_log_usage_clamped_at_zero(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    item_id = _seed_item(SessionLocal, current_stock=5)
    _login(test_client, "ops@example.com")

    test_client.post("/inventory/log-usage", data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 999}, follow_redirects=False)

    with SessionLocal() as session:
        assert session.get(InventoryItem, item_id).current_stock == 0


# --- Set stock ---

def test_set_stock_restock_creates_correction_entry(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    item_id = _seed_item(SessionLocal, current_stock=2)
    _login(test_client, "admin@example.com")

    resp = test_client.post(f"/inventory/{item_id}/set-stock", data={"new_stock": 50, "note": "restocked"}, follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        item = session.get(InventoryItem, item_id)
        assert item.current_stock == 50
        entry = session.execute(select(InventoryLogEntry).where(InventoryLogEntry.item_id == item_id)).scalar_one()
        assert entry.entry_type == "correction"
        assert entry.quantity_change == 48
        assert entry.resulting_stock == 50


def test_set_stock_downward_correction(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    item_id = _seed_item(SessionLocal, current_stock=50)
    _login(test_client, "admin@example.com")

    test_client.post(f"/inventory/{item_id}/set-stock", data={"new_stock": 10, "note": "recount"}, follow_redirects=False)

    with SessionLocal() as session:
        entry = session.execute(select(InventoryLogEntry).where(InventoryLogEntry.item_id == item_id)).scalar_one()
        assert entry.quantity_change == -40
        assert entry.resulting_stock == 10


# --- Dashboard rendering ---

def test_dashboard_shows_low_stock_badge(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_item(SessionLocal, key="dustbin_bags", name="Dustbin Bags", current_stock=2)  # real threshold is 5
    _login(test_client, "admin@example.com")

    resp = test_client.get("/inventory")
    assert "⚠ Low Stock" in resp.text
    assert "Dustbin Bags" in resp.text


def test_dashboard_shows_ok_badge_when_above_threshold(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_item(SessionLocal, key="dustbin_bags", current_stock=100)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/inventory")
    assert "🟢 OK" in resp.text
    assert "⚠ Low Stock" not in resp.text


def test_dashboard_shows_cartons_equivalent_for_glasses(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_item(SessionLocal, key="glasses", name="Glasses", unit="pieces", current_stock=50, pieces_per_carton=24)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/inventory")
    assert "2 cartons + 2 pcs" in resp.text


# --- Set Stock cartons support (2026-08-28) ---

def test_set_stock_form_shows_cartons_split_for_carton_item(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_item(SessionLocal, key="glasses", name="Glasses", unit="pieces", current_stock=50, pieces_per_carton=24)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/inventory")
    assert 'data-pieces-per-carton="24"' in resp.text
    assert 'class="set-stock-cartons"' in resp.text
    assert 'class="set-stock-loose"' in resp.text


def test_set_stock_form_shows_plain_input_for_non_carton_item(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _seed_item(SessionLocal, key="oranges", name="Oranges", unit="boxes", current_stock=20, pieces_per_carton=None)
    _login(test_client, "admin@example.com")

    resp = test_client.get("/inventory")
    # The Log Usage <select>'s data-pieces-per-carton="" is expected for
    # every item (empty string when unset), and the page's <script>
    # block always references the ".set-stock-cartons" CSS selector
    # regardless of items -- what must NOT appear is the actual rendered
    # cartons/loose INPUT elements, which only render when
    # pieces_per_carton is truthy.
    assert 'class="set-stock-cartons"' not in resp.text
    assert 'class="set-stock-loose"' not in resp.text
    assert 'name="new_stock"' in resp.text


# --- Crossing-triggered email, end to end through the real routes ---

def test_crossing_threshold_sends_exactly_one_email_across_two_requests(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    item_id = _seed_item(SessionLocal, key="dustbin_bags", current_stock=10)  # real threshold is 5
    _login(test_client, "admin@example.com")

    mock_smtp_ctx = MagicMock()
    with patch("services.notification_service.smtplib.SMTP", return_value=mock_smtp_ctx):
        with patch.object(backend_deps, "load_settings", lambda: replace(
            load_settings(), smtp_host="smtp.example.com", smtp_username="bot@example.com",
            smtp_password="secret", smtp_from_email="bot@example.com",
        )):
            # First request: 10 -> 6, still above threshold (5) -- no email.
            test_client.post("/inventory/log-usage", data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 4}, follow_redirects=False)
            # Second request: 6 -> 2, crosses below threshold -- one email.
            test_client.post("/inventory/log-usage", data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 4}, follow_redirects=False)
            # Third request: 2 -> 1, still below -- no additional email.
            test_client.post("/inventory/log-usage", data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 1}, follow_redirects=False)

    assert mock_smtp_ctx.__enter__.return_value.send_message.call_count == 1


# --- Cartons+pieces form arithmetic (server-side contract; JS itself isn't run under TestClient) ---

def test_glasses_usage_logs_correct_piece_total_from_cartons_and_loose(client):
    test_client, SessionLocal = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    item_id = _seed_item(SessionLocal, key="glasses", unit="pieces", current_stock=1000, pieces_per_carton=24)
    _login(test_client, "ops@example.com")

    # The template's JS computes quantity_used = cartons*pieces_per_carton + loose
    # before submit; TestClient bypasses JS, so post the already-computed
    # value directly (2 cartons * 24 + 5 loose = 53) to assert the
    # server-side contract the JS relies on.
    test_client.post("/inventory/log-usage", data={"item_id": item_id, "date": "2026-08-26", "quantity_used": 53}, follow_redirects=False)

    with SessionLocal() as session:
        assert session.get(InventoryItem, item_id).current_stock == 947
