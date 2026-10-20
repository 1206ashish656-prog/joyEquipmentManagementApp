"""FastAPI tests for the refund-check page (/refund-check) -- admin-only,
same TestClient + StaticPool SQLite pattern as test_reconciliation_backend.py.

PayU settings are overridden via FastAPI's own app.dependency_overrides
(not monkeypatch on backend.deps) -- same reasoning as
test_reconciliation_backend.py: the route module imported get_settings
as a plain function reference at module-load time, so patching
backend.deps.get_settings afterward would never be seen by the
already-bound route. The client fixture disables BOTH the production
PayU credentials (payu_merchant_key/salt) AND the test/sandbox
credentials (payu_test_merchant_key/salt) by default -- this project's
real .env has live credentials for both, and without disabling them
here, an un-mocked test could fire a real network call (REFUND_TRIGGER_ENABLED
defaults to True as of 2026-10-20 -- see its own comment in
backend/api/refund_check.py -- so an un-mocked POST test really would
reach PayU without this). Tests that exercise the trigger path
explicitly re-enable whichever credentials the scenario needs
(payu_merchant_key/salt for a normal/production-routed order,
payu_test_merchant_key/salt for a TEST_DEVICE_APP dummy order) AND
monkeypatch refund_check_module.PayUClient to a fake -- the same class
is used for every PayU call this route makes (the read-only lookups and
the trigger), so one fake covers both."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.api.refund_check as refund_check_module
import db.base as db_base
from backend.deps import get_settings
from backend.main import app
from backend.security import hash_password
from db.models import Base, OrderPaymentRecord, RefundCheck, UnrecordedPaymentCheck, User
from monitoring.config import load_settings
from monitoring.models import ExtractionError, TargetUnavailableError
from services.payu_client import PayURefund, PayURefundInitiationResult, PayUTransaction

OLD_ENOUGH = timedelta(hours=1)  # comfortably past the 150s MIN_ORDER_AGE_SECONDS gate


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    monkeypatch.setattr(refund_check_module, "_today_ist", lambda: "2026-08-29")
    _override_settings(
        payu_merchant_key="", payu_merchant_salt="",
        payu_test_merchant_key="", payu_test_merchant_salt="",
    )
    test_client = TestClient(app)
    try:
        yield test_client, SessionLocal, monkeypatch
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _add_user(SessionLocal, email, role, password="pw123456"):
    with SessionLocal() as session:
        user = User(name=email.split("@")[0], email=email, role=role, active=True, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        return user.id


def _login(client, email, password="pw123456"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def _add_order(
    SessionLocal, order_id, order_date, amount="120.00", payment_status="Have paid",
    order_status="Completed", delivery_status="Success", device_app="NEXUS", out_trade_no=None,
    pay_type="UPI", created_at_target=None,
):
    with SessionLocal() as session:
        session.add(OrderPaymentRecord(
            order_id=order_id, order_code=f"ORD{order_id}", device_app=device_app, order_date=order_date,
            order_money=Decimal(amount), pay_type=pay_type, payment_status=payment_status,
            order_status=order_status, delivery_status=delivery_status, out_trade_no=out_trade_no,
            created_at_target=created_at_target or (datetime.now(timezone.utc) - OLD_ENOUGH),
        ))
        session.commit()


def _override_settings(**overrides):
    settings = replace(load_settings(), **overrides)
    app.dependency_overrides[get_settings] = lambda: settings


def test_requires_login(client):
    test_client, _, _ = client
    resp = test_client.get("/refund-check", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_operations_cannot_view(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    resp = test_client.get("/refund-check")
    assert resp.status_code == 403


def test_shows_only_paid_but_failed_orders_within_lookback(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")  # in range, failed -- shown
    _add_order(SessionLocal, "2", "2026-08-23", order_status="Cancel")  # in range (7-day lookback, T-6), failed -- shown
    _add_order(SessionLocal, "3", "2026-08-22", delivery_status="Failure")  # out of range (T-7) -- hidden
    _add_order(SessionLocal, "4", "2026-08-29")  # fully successful -- hidden
    _add_order(SessionLocal, "5", "2026-08-29", payment_status="Non-payment", order_status="Cancel")  # never paid -- hidden
    _add_order(SessionLocal, "6", "2026-08-29", delivery_status="Failure", pay_type="Self-check repair")  # non-UPI -- hidden

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "ORD1" in resp.text
    assert "ORD2" in resp.text
    assert "ORD3" not in resp.text
    assert "ORD4" not in resp.text
    assert "ORD5" not in resp.text
    assert "ORD6" not in resp.text


def test_order_younger_than_150s_is_excluded(client):
    """The realtime age gate: an order created less than 150s ago is
    excluded even if it already looks like a failed delivery -- juice
    dispensing takes ~120s, so this may still just be mid-dispense."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", created_at_target=datetime.now(timezone.utc) - timedelta(seconds=140))
    _add_order(SessionLocal, "2", "2026-08-29", delivery_status="Failure", created_at_target=datetime.now(timezone.utc) - timedelta(seconds=200))

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "ORD1" not in resp.text  # 140s old -- too young
    assert "ORD2" in resp.text  # 200s old -- eligible


def test_no_failed_transactions_shows_empty_message(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "No failed transactions found" in resp.text


def test_operations_cannot_trigger_refund(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "ops@example.com", "operations")
    _login(test_client, "ops@example.com")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")

    resp = test_client.post("/refund-check/1/refund")
    assert resp.status_code == 403


# --- PayU cross-reference (payu_id / payu_payment_status columns) ---

def test_shows_setup_message_when_payu_not_configured(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "PAYU_MERCHANT_KEY" in resp.text


def test_payu_status_shown_for_matching_out_trade_no(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="2026102010054555", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="dropped", mode="UPI", addedon="2026-08-29 10:00", bank_ref_no="BANKREF789", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "PAYU_ID_1" in resp.text
    assert "dropped" in resp.text
    assert "BANKREF789" in resp.text


def test_payu_fetch_failure_shows_error_not_crash(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FailingPayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            raise ExtractionError("PayU API error: Invalid hash")

        async def get_refunds_for_transaction(self, txnid):
            # get_transaction_details and get_refunds_for_transaction are
            # independent calls (2026-10-20 refactor) -- one failing must
            # not block the other, so this still gets called even though
            # the fetch above raised.
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FailingPayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "Could not fetch PayU status" in resp.text
    assert "ORD1" in resp.text


# --- refund verification display (Refund ID / Refund Status, from
# PayU's getAllRefundsFromTxnIds -- keyed by order_code/txnid) ---

def test_refund_shown_when_payu_confirms_one(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="2026-08-29 10:00", bank_ref_no="BANKREF789", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            assert txnid == "ORD1"
            return [PayURefund(
                refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                created_at="2026-08-29 11:00:00", bank_ref_no="", raw={},
            )]

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "TOKEN999" in resp.text
    assert "success" in resp.text
    assert "Refunded (PayU confirmed)" in resp.text
    # The Refund button must not appear once PayU confirms a refund.
    assert 'action="/refund-check/1/refund"' not in resp.text


def test_no_refund_id_shown_when_payu_has_none(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", True)

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="2026-08-29 10:00", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []  # "No Refunds Found" -- the normal case

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "TOKEN999" not in resp.text
    assert 'action="/refund-check/1/refund"' in resp.text  # button still shown -- nothing to disable it yet


def test_payu_refund_lookup_only_called_for_failed_transactions(client, monkeypatch):
    """No point spending a PayU call per order on a page scoped to
    failed transactions -- only failed-transaction order_codes should
    ever be looked up."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")  # order_code "ORD1" -- failed, looked up
    _add_order(SessionLocal, "2", "2026-08-29")  # order_code "ORD2" -- fully successful, excluded from the page
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    looked_up = []

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            looked_up.append(txnid)
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert looked_up == ["ORD1"]


def test_payu_refund_lookup_failure_for_one_row_does_not_crash_page(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, payu_id):
            raise ExtractionError("PayU API error: boom")

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "ORD1" in resp.text


def test_write_through_persists_live_refund_into_refund_check(client, monkeypatch):
    """GET's write-through cache: a refund PayU already shows (e.g. made
    directly on PayU's dashboard, never through this app) is persisted
    into refund_check on the next page view, without needing a POST."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return [PayURefund(
                refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                created_at="2026-08-29 11:00:00", bank_ref_no="", raw={},
            )]

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.payu_refund_token == "TOKEN999"
        assert rc.payu_refund_status == "success"
        assert rc.payu_refund_checked_at is not None
        # Display-only -- a passive page view must never set the
        # idempotency lock; only a POST does.
        assert rc.refund_api_requested_at is None


# --- refund-trigger pipeline (POST /refund-check/{id}/refund) ---

def _fake_payu_client(initiate_result=None, initiate_exception=None, existing_refunds=None):
    """Builds a FakePayUClient class covering every PayU call this route
    makes: get_transaction_details (GET only), get_refunds_for_transaction
    (both GET's display lookup and POST's pre-trigger idempotency check),
    and initiate_refund (POST's trigger)."""
    class FakePayUClient:
        calls: list = []

        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return list(existing_refunds or [])

        async def initiate_refund(self, mihpayid, token, amount):
            FakePayUClient.calls.append((mihpayid, token, amount))
            if initiate_exception is not None:
                raise initiate_exception
            return initiate_result

        async def close(self):
            pass

    return FakePayUClient


def test_refund_trigger_success_locks_and_shows_pending(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    fake = _fake_payu_client(initiate_result=PayURefundInitiationResult(
        success=True, request_id="PAYU_REQ_1", message="Refund Request Queued", mihpayid="PAYU_ID_1", raw={},
    ))
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    resp = test_client.post("/refund-check/1/refund", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_requested is True
        assert rc.refund_api_requested_at is not None
        assert rc.refund_mode == "production"
        assert rc.payu_refund_status == "queued"
        assert rc.refund_api_error is None
        assert fake.calls == [("PAYU_ID_1", "RF1", Decimal("120.00"))]

    resp = test_client.get("/refund-check")
    assert "Refund Requested — pending PayU confirmation" in resp.text
    assert 'action="/refund-check/1/refund"' not in resp.text  # locked -- no button


def test_refund_trigger_rejection_locks_and_shows_error(client, monkeypatch):
    """PayU's own rejection (e.g. invalid params) is still a definitive
    response -- locks idempotency the same as success, per explicit
    request ("if refund request is already sent... ignores in backend")."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    fake = _fake_payu_client(initiate_result=PayURefundInitiationResult(
        success=False, request_id=None, message="token already used or request pending", mihpayid="PAYU_ID_1", raw={},
    ))
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is not None  # still locked
        assert rc.refund_api_error == "token already used or request pending"

    resp = test_client.get("/refund-check")
    assert "Refund Failed" in resp.text
    assert 'action="/refund-check/1/refund"' not in resp.text  # locked, even though it failed


def test_refund_trigger_network_error_does_not_lock(client, monkeypatch):
    """A transport failure before any PayU response must NOT lock --
    the admin should be able to retry once the network issue clears."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    fake = _fake_payu_client(initiate_exception=TargetUnavailableError("connection refused"))
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is None  # NOT locked
        assert "connection refused" in rc.refund_api_error

    resp = test_client.get("/refund-check")
    assert 'action="/refund-check/1/refund"' in resp.text  # button still shown -- retryable


def test_refund_trigger_idempotent_second_post_does_not_call_payu_again(client, monkeypatch):
    """The core idempotency requirement: once a request reached PayU,
    a repeat POST must be a silent no-op, never a second API call."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    fake = _fake_payu_client(initiate_result=PayURefundInitiationResult(
        success=True, request_id="PAYU_REQ_1", message="Refund Request Queued", mihpayid="PAYU_ID_1", raw={},
    ))
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    test_client.post("/refund-check/1/refund", follow_redirects=False)
    test_client.post("/refund-check/1/refund", follow_redirects=False)
    test_client.post("/refund-check/1/refund", follow_redirects=False)

    assert len(fake.calls) == 1  # only the first POST actually called PayU


def test_refund_trigger_backfills_when_payu_already_shows_a_refund(client, monkeypatch):
    """The exact real-world case: a refund already exists on PayU
    (e.g. made directly on PayU's dashboard) -- clicking Refund in this
    app must record it, not send a second refund."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    existing_refund = PayURefund(
        refund_token="31386182688_506445f139", request_id="REQ1", amount=Decimal("120.00"),
        status="success", created_at="2026-10-19 22:51:47", bank_ref_no="", raw={},
    )
    fake = _fake_payu_client(existing_refunds=[existing_refund])
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    resp = test_client.post("/refund-check/1/refund", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is not None  # locked
        assert rc.payu_refund_token == "31386182688_506445f139"
        assert rc.payu_refund_status == "success"
        assert rc.refund_mode == "production"
    assert fake.calls == []  # initiate_refund was NEVER called


def test_refund_trigger_without_production_credentials_shows_error_and_does_not_lock(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    # client fixture already leaves payu_merchant_key/salt empty, and
    # this order isn't TEST_DEVICE_APP, so it needs production credentials.

    resp = test_client.post("/refund-check/1/refund", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is None
        assert "PAYU_MERCHANT_KEY" in rc.refund_api_error


def test_refund_button_has_confirm_dialog(client):
    """The 'admin authorizes it' gesture -- a plain POST form with a JS
    confirm() before it submits, no separate re-auth step. Renders this
    way whenever REFUND_TRIGGER_ENABLED is True, which is the real,
    shipped default as of 2026-10-20 (production account activated, per
    explicit instruction) -- see the explicitly-disabled tests below for
    the kill-switch's own posture."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/refund-check")
    assert "onsubmit=\"return confirm(" in resp.text


def test_refund_trigger_uses_production_credentials_for_a_normal_order(client, monkeypatch):
    """The activated posture (2026-10-20): a normal (non-TEST_DEVICE_APP)
    order's trigger call must resolve PRODUCTION credentials, not test
    ones -- this is the real-money path the admin explicitly asked to
    activate."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(
        payu_merchant_key="REAL_PRODUCTION_KEY", payu_merchant_salt="REAL_PRODUCTION_SALT", payu_env="production",
        payu_test_merchant_key="testkey", payu_test_merchant_salt="testsalt",
    )

    seen_settings = []

    class FakePayUClient:
        def __init__(self, settings):
            seen_settings.append(settings)

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def initiate_refund(self, mihpayid, token, amount):
            return PayURefundInitiationResult(success=True, request_id="R1", message="ok", mihpayid=mihpayid, raw={})

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    trigger_settings = seen_settings[-1]  # the client constructed for initiate_refund
    assert trigger_settings.payu_merchant_key == "REAL_PRODUCTION_KEY"
    assert trigger_settings.payu_merchant_salt == "REAL_PRODUCTION_SALT"
    assert trigger_settings.payu_merchant_key != "testkey"

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_mode == "production"


def test_refund_trigger_uses_test_credentials_for_test_device_app_order(client, monkeypatch):
    """The dormant verification path stays isolated: a TEST_DEVICE_APP
    dummy order's trigger call must still resolve test/sandbox
    credentials, never the real production ones, even though production
    is now the default for every other order."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(
        SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="SANDBOX_MIHPAYID",
        device_app=refund_check_module.TEST_DEVICE_APP,
    )
    _login(test_client, "admin@example.com")
    _override_settings(
        payu_merchant_key="REAL_PRODUCTION_KEY", payu_merchant_salt="REAL_PRODUCTION_SALT", payu_env="production",
        payu_test_merchant_key="testkey", payu_test_merchant_salt="testsalt",
    )

    seen_settings = []

    class FakePayUClient:
        def __init__(self, settings):
            seen_settings.append(settings)

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def initiate_refund(self, mihpayid, token, amount):
            return PayURefundInitiationResult(success=True, request_id="R1", message="ok", mihpayid=mihpayid, raw={})

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    trigger_settings = seen_settings[-1]
    assert trigger_settings.payu_merchant_key == "testkey"
    assert trigger_settings.payu_merchant_salt == "testsalt"
    assert trigger_settings.payu_merchant_key != "REAL_PRODUCTION_KEY"

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_mode == "test"


# --- dummy TEST_DEVICE_APP orders -- cross-referenced against the TEST
# PayU account, not production, since that's where their records live ---

def test_test_device_app_order_cross_referenced_against_test_account(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(
        SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="SANDBOX_MIHPAYID",
        device_app=refund_check_module.TEST_DEVICE_APP,
    )
    _add_order(SessionLocal, "2", "2026-08-29", delivery_status="Failure", out_trade_no="PROD_MIHPAYID")  # real order, default device_app
    _login(test_client, "admin@example.com")
    _override_settings(
        payu_merchant_key="prodkey", payu_merchant_salt="prodsalt",
        payu_test_merchant_key="sandboxkey", payu_test_merchant_salt="sandboxsalt",
    )

    calls = []

    class FakePayUClient:
        def __init__(self, settings):
            self.settings = settings

        async def get_transaction_details(self, start, end):
            calls.append(("get_transaction_details", self.settings.payu_merchant_key))
            if self.settings.payu_merchant_key == "sandboxkey":
                return [PayUTransaction(
                    txnid="ORD1", mihpayid="SANDBOX_MIHPAYID", amount=Decimal("1.00"),
                    status="captured", mode="UPI", addedon="", bank_ref_no="SANDBOXREF", raw={},
                )]
            return [PayUTransaction(
                txnid="ORD2", mihpayid="PROD_MIHPAYID", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="PRODREF", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            calls.append(("get_refunds_for_transaction", self.settings.payu_merchant_key, txnid))
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "SANDBOXREF" in resp.text  # the TEST_DEVICE_APP order's own PayU data
    assert "PRODREF" in resp.text  # the real order's own PayU data

    # Each order's refund lookup used the right account's credentials.
    refund_calls = {c for c in calls if c[0] == "get_refunds_for_transaction"}
    assert ("get_refunds_for_transaction", "sandboxkey", "ORD1") in refund_calls
    assert ("get_refunds_for_transaction", "prodkey", "ORD2") in refund_calls


def test_test_device_app_order_trigger_pre_check_uses_test_account(client, monkeypatch):
    """The POST handler's pre-trigger idempotency check must look at the
    TEST account for a dummy order, not production -- production would
    never have a sandbox txnid, so it would always wrongly say 'no
    existing refund' and let a duplicate-looking trigger through."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(
        SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="SANDBOX_MIHPAYID",
        device_app=refund_check_module.TEST_DEVICE_APP,
    )
    _login(test_client, "admin@example.com")
    _override_settings(
        payu_merchant_key="prodkey", payu_merchant_salt="prodsalt",
        payu_test_merchant_key="sandboxkey", payu_test_merchant_salt="sandboxsalt",
    )

    pre_check_calls = []

    class FakePayUClient:
        def __init__(self, settings):
            self.settings = settings

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            pre_check_calls.append(self.settings.payu_merchant_key)
            # PayU already shows this sandbox order as refunded.
            return [PayURefund(
                refund_token="SANDBOX_TOKEN", request_id="R1", amount=Decimal("1.00"),
                status="success", created_at="", bank_ref_no="", raw={},
            )]

        async def initiate_refund(self, mihpayid, token, amount):
            raise AssertionError("must not call initiate_refund -- PayU already shows a refund (backfill expected)")

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.post("/refund-check/1/refund", follow_redirects=False)
    assert resp.status_code == 303
    assert pre_check_calls == ["sandboxkey"]  # looked at the TEST account, not production

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.payu_refund_token == "SANDBOX_TOKEN"
        assert rc.refund_api_requested_at is not None


# --- refund_trace (step-by-step log, per explicit request to "see
# steps in the refund process... show which endpoints hit") ---

def test_trigger_success_trace_shows_both_steps(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    fake = _fake_payu_client(initiate_result=PayURefundInitiationResult(
        success=True, request_id="PAYU_REQ_1", message="Refund Request Queued", mihpayid="PAYU_ID_1", raw={},
    ))
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        trace = rc.refund_trace
        assert "clicked by admin@example.com" in trace
        assert "Step 1: checking for an existing refund — getAllRefundsFromTxnIds" in trace
        assert "no existing refund found" in trace
        assert "Step 2: triggering refund — cancel_refund_transaction" in trace
        assert "mihpayid=PAYU_ID_1" in trace
        assert "PayU accepted the request" in trace
        assert "request_id=PAYU_REQ_1" in trace

    resp = test_client.get("/refund-check")
    assert "Steps" in resp.text  # the <details><summary> trace toggle


def test_backfill_trace_shows_no_trigger_call_was_made(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    existing_refund = PayURefund(
        refund_token="EXISTING_TOKEN", request_id="R1", amount=Decimal("120.00"),
        status="success", created_at="", bank_ref_no="", raw={},
    )
    fake = _fake_payu_client(existing_refunds=[existing_refund])
    monkeypatch.setattr(refund_check_module, "PayUClient", fake)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        trace = rc.refund_trace
        assert "found existing refund: token=EXISTING_TOKEN" in trace
        assert "cancel_refund_transaction was NOT called" in trace


# --- REFUND_TRIGGER_ENABLED kill-switch -- ACTIVE (True) as of
# 2026-10-20, per explicit instruction ("Activate the functionality in
# the current setup keeping the constraint on refund authorization
# intact" -> "route the actual refund call at the production PayU
# account (real refunds, real money)"). Most tests above rely on this
# real default rather than monkeypatching it. These verify both the
# real default itself AND that explicitly flipping it back to False
# (e.g. to pause the pipeline again) still fully disables the trigger,
# the same way it did before this activation. ---

def test_refund_trigger_enabled_by_default(client):
    """REFUND_TRIGGER_ENABLED defaults to True with no monkeypatch at
    all -- this is the real, shipped (2026-10-20) default, not just a
    test fixture. The admin-authorization constraint (require_admin +
    the button's confirm() dialog) is what keeps this safe, not the
    flag -- see module docstring."""
    assert refund_check_module.REFUND_TRIGGER_ENABLED is True


def test_disabled_button_shown_when_explicitly_disabled(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")
    _login(test_client, "admin@example.com")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert '<button type="button" class="linklike" disabled' in resp.text
    assert 'action="/refund-check/1/refund"' not in resp.text
    assert "onsubmit=\"return confirm(" not in resp.text
    assert "Refund triggering is currently disabled" in resp.text


def test_already_refunded_order_still_shown_as_refunded_regardless_of_kill_switch(client, monkeypatch):
    """The kill-switch only blocks the TRIGGER -- an order PayU already
    shows as refunded must still display that way either way, since
    this is read-only status display, not refund functionality."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return [PayURefund(
                refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                created_at="", bank_ref_no="", raw={},
            )]

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert "Refunded (PayU confirmed)" in resp.text
    assert "TOKEN999" in resp.text


# --- hide-refunded split (2026-10-21, per explicit request: "show only
# those orders for which refund is not triggered and hide orders that
# already received refund... allow user to unhide refunded
# transactions") ---

def test_refunded_order_hidden_in_collapsed_section_non_refunded_in_main_table(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")  # stays unrefunded
    _add_order(SessionLocal, "2", "2026-08-29", order_status="Cancel", out_trade_no="PAYU_ID_2")  # already refunded
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            if txnid == "ORD2":
                return [PayURefund(
                    refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                    created_at="", bank_ref_no="", raw={},
                )]
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert "Show 1 refunded order" in resp.text
    # ORD1 (unrefunded) must appear BEFORE the "Show refunded" toggle --
    # i.e. in the always-visible main table, not the collapsed section.
    assert resp.text.index("ORD1") < resp.text.index("Show 1 refunded order")
    # ORD2 (refunded) must appear AFTER it -- inside the collapsed section.
    assert resp.text.index("Show 1 refunded order") < resp.text.index("ORD2")
    # The collapsed <details> is NOT open when there's no active search.
    assert '<details style="margin-top:0.75em;" >' in resp.text
    assert "open>" not in resp.text


def test_search_auto_opens_collapsed_section_when_it_matches_a_refunded_order(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", order_status="Cancel", out_trade_no="PAYU_ID_1")  # refunded
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="BANKREF_FINDME", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return [PayURefund(
                refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                created_at="", bank_ref_no="", raw={},
            )]

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check", params={"search": "BANKREF_FINDME"})
    assert resp.status_code == 200
    assert "ORD1" in resp.text  # the refunded order is still findable by its bank ref
    assert "<details" in resp.text and "open>" in resp.text  # auto-expanded, not left collapsed


def test_search_matches_zero_refunded_leaves_section_collapsed(client, monkeypatch):
    """A search that doesn't match anything in the refunded group must
    not force it open for no reason."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", order_status="Cancel", out_trade_no="PAYU_ID_1")  # refunded
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return []

        async def get_refunds_for_transaction(self, txnid):
            return [PayURefund(
                refund_token="TOKEN999", request_id="REQ999", amount=Decimal("120.00"), status="success",
                created_at="", bank_ref_no="SOMETHING_ELSE", raw={},
            )]

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check", params={"search": "no-such-match-anywhere"})
    assert resp.status_code == 200
    assert "ORD1" not in resp.text  # filtered out of the refunded section entirely
    assert "open>" not in resp.text  # nothing to unhide, so nothing auto-opens


def test_post_while_explicitly_disabled_makes_zero_payu_calls(client, monkeypatch):
    """The strongest guarantee: a direct POST while the kill-switch is
    off must not reach PayU at all -- not even the pre-trigger
    idempotency lookup."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure", out_trade_no="PAYU_ID_1")
    _login(test_client, "admin@example.com")
    _override_settings(
        payu_merchant_key="prodkey", payu_merchant_salt="prodsalt",
        payu_test_merchant_key="testkey", payu_test_merchant_salt="testsalt",
    )
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    def _unexpected_construction(settings):
        raise AssertionError("PayUClient must never be constructed while REFUND_TRIGGER_ENABLED is False")

    monkeypatch.setattr(refund_check_module, "PayUClient", _unexpected_construction)

    resp = test_client.post("/refund-check/1/refund", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is None
        assert rc.refund_mode is None
        assert "refund triggering is currently disabled" in rc.refund_trace.lower()


def test_post_while_explicitly_disabled_does_not_set_local_intent_flag(client, monkeypatch):
    """A disabled click shouldn't even record the usual 'admin clicked
    Refund' intent -- there's nothing to act on, so nothing to flag."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")
    _login(test_client, "admin@example.com")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    test_client.post("/refund-check/1/refund", follow_redirects=False)

    with SessionLocal() as session:
        rc = session.execute(select(RefundCheck).where(RefundCheck.order_id == "1")).scalar_one()
        assert rc.refund_requested is False


def test_production_refund_callout_shown_when_enabled(client):
    """The activated (default) posture's callout must accurately say
    this now moves real money against production -- not the earlier
    TEST/sandbox-only wording."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/refund-check")
    assert "production" in resp.text
    assert "moves real money" in resp.text
    assert "TEST/sandbox PayU account" not in resp.text


def test_disabled_callout_shown_when_explicitly_disabled(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    resp = test_client.get("/refund-check")
    assert "live production PayU data" in resp.text
    assert "is currently disabled" in resp.text


# --- unrecorded payments (the inverse: PayU shows paid, machine says
# it isn't -- per explicit request, "received payment as per payU but
# didn't receive payment by the machine") ---

def test_unrecorded_payment_shown_and_matched_by_order_code(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Non-payment")  # order_code "ORD1"
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="2026-08-29 10:00", bank_ref_no="BANKREF789", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert resp.status_code == 200
    assert "Payment Received by PayU but Not by Machine" in resp.text
    assert "PAYU_ID_1" in resp.text
    assert "No PayU-paid-but-machine-unpaid orders found" not in resp.text


def test_unrecorded_payment_not_shown_when_machine_also_says_paid(client, monkeypatch):
    """A normal matched order (payment_status == 'Have paid') must never
    appear in this table -- it's reconciliation.py's territory."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Have paid")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert "No PayU-paid-but-machine-unpaid orders found" in resp.text


def test_unrecorded_payment_empty_message_shown(client):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _login(test_client, "admin@example.com")

    resp = test_client.get("/refund-check")
    assert "No PayU-paid-but-machine-unpaid orders found" in resp.text


def test_unrecorded_payment_disabled_button_when_explicitly_disabled(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Non-payment")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    assert 'action="/refund-check/unrecorded-payment/1/refund"' not in resp.text
    assert '<button type="button" class="linklike" disabled' in resp.text


def test_unrecorded_payment_trigger_explicitly_disabled_makes_zero_calls(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Non-payment")
    _login(test_client, "admin@example.com")
    monkeypatch.setattr(refund_check_module, "REFUND_TRIGGER_ENABLED", False)

    def _unexpected_construction(settings):
        raise AssertionError("PayUClient must never be constructed while REFUND_TRIGGER_ENABLED is False")

    monkeypatch.setattr(refund_check_module, "PayUClient", _unexpected_construction)

    resp = test_client.post("/refund-check/unrecorded-payment/1/refund", follow_redirects=False)
    assert resp.status_code == 303

    with SessionLocal() as session:
        rc = session.execute(select(UnrecordedPaymentCheck).where(UnrecordedPaymentCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is None
        assert "refund triggering is currently disabled" in rc.refund_trace.lower()


def test_unrecorded_payment_trigger_success_resolves_mihpayid_from_matched_transaction(client, monkeypatch):
    """There's no reliable out_trade_no here (that's exactly what's in
    question) -- the refund target mihpayid must come from the PayU
    transaction actually matched via order_code/txnid."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Non-payment", amount="135.00")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    initiate_calls = []

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="RESOLVED_MIHPAYID", amount=Decimal("135.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def initiate_refund(self, mihpayid, token, amount):
            initiate_calls.append((mihpayid, token, amount))
            return PayURefundInitiationResult(success=True, request_id="R1", message="Refund Request Queued", mihpayid=mihpayid, raw={})

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.post("/refund-check/unrecorded-payment/1/refund", follow_redirects=False)
    assert resp.status_code == 303
    assert initiate_calls == [("RESOLVED_MIHPAYID", "RF1", Decimal("135.00"))]

    with SessionLocal() as session:
        rc = session.execute(select(UnrecordedPaymentCheck).where(UnrecordedPaymentCheck.order_id == "1")).scalar_one()
        assert rc.refund_api_requested_at is not None
        assert rc.payu_refund_status == "queued"
        assert rc.refund_mode == "production"


def test_unrecorded_payment_trigger_idempotent(client, monkeypatch):
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", payment_status="Non-payment")
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="prodkey", payu_merchant_salt="prodsalt")

    initiate_calls = []

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD1", mihpayid="PAYU_ID_1", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def initiate_refund(self, mihpayid, token, amount):
            initiate_calls.append(1)
            return PayURefundInitiationResult(success=True, request_id="R1", message="ok", mihpayid=mihpayid, raw={})

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    test_client.post("/refund-check/unrecorded-payment/1/refund", follow_redirects=False)
    test_client.post("/refund-check/unrecorded-payment/1/refund", follow_redirects=False)

    assert len(initiate_calls) == 1


def test_unrecorded_payment_and_failed_transaction_tables_are_independent(client, monkeypatch):
    """Sanity check against the real running page: an order appears in
    at most one of the two tables, never both, and a POST to one
    endpoint never touches the other table's row."""
    test_client, SessionLocal, _ = client
    _add_user(SessionLocal, "admin@example.com", "admin")
    _add_order(SessionLocal, "1", "2026-08-29", delivery_status="Failure")  # failed transaction
    _add_order(SessionLocal, "2", "2026-08-29", payment_status="Non-payment")  # unrecorded payment
    _login(test_client, "admin@example.com")
    _override_settings(payu_merchant_key="key", payu_merchant_salt="salt")

    class FakePayUClient:
        def __init__(self, settings):
            pass

        async def get_transaction_details(self, start, end):
            return [PayUTransaction(
                txnid="ORD2", mihpayid="PAYU_ID_2", amount=Decimal("120.00"),
                status="captured", mode="UPI", addedon="", bank_ref_no="", raw={},
            )]

        async def get_refunds_for_transaction(self, txnid):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(refund_check_module, "PayUClient", FakePayUClient)

    resp = test_client.get("/refund-check")
    # ORD1 (failed transaction) appears once, in the first table; ORD2
    # (unrecorded payment) appears once, in the second table.
    assert resp.text.count("ORD1") == 1
    assert resp.text.count("ORD2") == 1
