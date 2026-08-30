"""FastAPI tests for the reconciliation page (/reconciliation) --
admin-only, same TestClient + StaticPool SQLite pattern as
test_costs_backend.py. The real PayU network call
(services.payu_client.PayUClient) is monkeypatched here -- there are no
real PayU credentials in this environment, and even if there were, a
unit suite should never depend on a live third-party payment gateway.

Settings are overridden via FastAPI's own app.dependency_overrides
(not monkeypatch on backend.deps) -- the route module imported
get_settings as a plain function reference at module-load time, so
patching backend.deps.get_settings afterward would never be seen by
the already-bound route; dependency_overrides is what FastAPI itself
provides for exactly this."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.api.reconciliation as reconciliation_module
import db.base as db_base
from backend.deps import get_settings
from backend.main import app
from backend.security import hash_password
from db.models import Base, OrderPaymentRecord, User
from monitoring.config import load_settings
from monitoring.models import ExtractionError
from services.payu_client import PayUTransaction


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
    try:
        yield test_client, SessionLocal, monkeypatch
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        session.add(User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password)))
        session.commit()


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_order(SessionLocal, order_id, out_trade_no, amount="120.00", payment_status="Have paid", pay_type="UPI", order_date="2026-08-29"):
    with SessionLocal() as session:
        session.add(OrderPaymentRecord(
            order_id=order_id, order_code=f"ORD{order_id}", device_app="NEXUS", order_date=order_date,
            order_money=Decimal(amount), pay_type=pay_type, payment_status=payment_status,
            out_trade_no=out_trade_no, created_at_target=datetime.now(timezone.utc),
        ))
        session.commit()


def _override_settings(**overrides):
    settings = replace(load_settings(), **overrides)
    app.dependency_overrides[get_settings] = lambda: settings


def test_requires_login(client):
    test_client, _, _ = client
    resp = test_client.get("/reconciliation", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/reconciliation")
    assert resp.status_code == 403


def test_shows_setup_message_when_payu_not_configured(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="", payu_merchant_salt="")

    resp = test_client.get("/reconciliation")
    assert resp.status_code == 200
    assert "PAYU_MERCHANT_KEY" in resp.text


def test_run_with_matched_order(client):
    test_client, SessionLocal, monkeypatch = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "TXN1", amount="120.00")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(txnid="TXN1", mihpayid="P1", amount=Decimal("120.00"), status="success", mode="UPI", addedon="", bank_ref_no="", raw={})]

        async def close(self):
            pass

    monkeypatch.setattr(reconciliation_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/reconciliation?start=2026-08-01&end=2026-08-31&run=1")
    assert resp.status_code == 200
    assert "Matched" in resp.text
    assert "No discrepancies found" in resp.text


def test_run_with_mismatch_shows_problem(client):
    test_client, SessionLocal, monkeypatch = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "TXN1", amount="120.00")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(txnid="TXN1", mihpayid="P1", amount=Decimal("100.00"), status="success", mode="UPI", addedon="", bank_ref_no="", raw={})]

        async def close(self):
            pass

    monkeypatch.setattr(reconciliation_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/reconciliation?start=2026-08-01&end=2026-08-31&run=1")
    assert resp.status_code == 200
    assert "Amount Mismatch" in resp.text


def test_payu_fetch_failure_shows_error_not_crash(client):
    test_client, SessionLocal, monkeypatch = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FailingPayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            raise ExtractionError("PayU API error: Invalid hash")

        async def close(self):
            pass

    monkeypatch.setattr(reconciliation_module, "PayUClient", FailingPayUClient)

    resp = test_client.get("/reconciliation?start=2026-08-01&end=2026-08-31&run=1")
    assert resp.status_code == 200
    assert "Could not fetch PayU transactions" in resp.text


def test_does_not_call_payu_without_run_param(client):
    """Nothing calls the paid external API just from loading the page --
    same "admin must explicitly trigger" caution as every other
    external-service action in this app."""
    test_client, SessionLocal, monkeypatch = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    called = []

    class TrackingPayUClient:
        def __init__(self, settings):
            called.append(True)

        async def get_transaction_details(self, start, end):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(reconciliation_module, "PayUClient", TrackingPayUClient)

    resp = test_client.get("/reconciliation")
    assert resp.status_code == 200
    assert called == []


def test_run_with_missing_on_machine(client):
    test_client, SessionLocal, monkeypatch = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(txnid="ORPHAN", mihpayid="P9", amount=Decimal("50.00"), status="success", mode="UPI", addedon="", bank_ref_no="", raw={})]

        async def close(self):
            pass

    monkeypatch.setattr(reconciliation_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/reconciliation?start=2026-08-01&end=2026-08-31&run=1")
    assert resp.status_code == 200
    assert "Missing on Machine Server" in resp.text
    assert "ORPHAN" in resp.text
