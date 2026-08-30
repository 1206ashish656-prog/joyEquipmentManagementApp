"""
Unit tests for services/reconciliation.py's pure matching logic --
no DB, no PayU network call, just OrderPaymentRecord + PayUTransaction
objects in and a ReconciliationReport out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from db.models import OrderPaymentRecord
from services.payu_client import PayUTransaction
from services.reconciliation import (
    AMOUNT_MISMATCH,
    MATCHED,
    MISSING_IN_PAYU,
    MISSING_ON_MACHINE,
    build_reconciliation,
)


def _order(
    order_id="1", order_code="ORD1", device_app="NEXUS", order_date="2026-08-29",
    order_money="120.00", pay_type="UPI", payment_status="Have paid", out_trade_no="TXN1",
):
    return OrderPaymentRecord(
        order_id=order_id, order_code=order_code, device_app=device_app, order_date=order_date,
        order_money=Decimal(order_money), pay_type=pay_type, payment_status=payment_status,
        out_trade_no=out_trade_no, created_at_target=datetime.now(timezone.utc),
    )


def _payu_txn(txnid="TXN1", amount="120.00", status="success", mihpayid="PAYU1"):
    return PayUTransaction(txnid=txnid, mihpayid=mihpayid, amount=Decimal(amount), status=status, mode="UPI", addedon="", bank_ref_no="", raw={})


def test_matched_order_and_transaction():
    report = build_reconciliation([_order()], [_payu_txn()], "2026-08-01", "2026-08-31")
    assert report.matched_count == 1
    assert report.rows[0].match_status == MATCHED
    assert not report.has_problems


def test_amount_mismatch_flagged():
    report = build_reconciliation([_order(order_money="120.00")], [_payu_txn(amount="100.00")], "2026-08-01", "2026-08-31")
    assert report.mismatch_count == 1
    assert report.rows[0].match_status == AMOUNT_MISMATCH
    assert report.has_problems


def test_paid_order_missing_from_payu_flagged():
    report = build_reconciliation([_order(payment_status="Have paid", out_trade_no="TXN1")], [], "2026-08-01", "2026-08-31")
    assert report.missing_in_payu_count == 1
    assert report.rows[0].match_status == MISSING_IN_PAYU


def test_unpaid_order_missing_from_payu_is_not_flagged():
    """A genuinely abandoned/failed checkout with no payment attempt on
    either side is not a reconciliation problem."""
    report = build_reconciliation([_order(payment_status="Non-payment", out_trade_no="")], [], "2026-08-01", "2026-08-31")
    assert report.rows == []
    assert not report.has_problems


def test_successful_payu_transaction_missing_on_machine_flagged():
    report = build_reconciliation([], [_payu_txn(txnid="ORPHAN", status="success")], "2026-08-01", "2026-08-31")
    assert report.missing_on_machine_count == 1
    assert report.rows[0].match_status == MISSING_ON_MACHINE
    assert report.rows[0].out_trade_no == "ORPHAN"


def test_failed_payu_transaction_with_no_order_not_flagged():
    """A failed PayU attempt with no corresponding order isn't a
    reconciliation problem -- it's just an abandoned checkout PayU saw
    but the machine never recorded a payment for."""
    report = build_reconciliation([], [_payu_txn(txnid="X", status="failure")], "2026-08-01", "2026-08-31")
    assert report.rows == []


def test_non_upi_order_excluded_entirely():
    """"Self-check repair" orders never reach PayU -- must never appear
    as a false "missing" row even if PayU coincidentally has no
    matching txnid (which it never would)."""
    report = build_reconciliation(
        [_order(pay_type="Self-check repair", payment_status="Have paid", out_trade_no="")],
        [], "2026-08-01", "2026-08-31",
    )
    assert report.rows == []


def test_matched_transaction_not_also_flagged_as_missing_on_machine():
    """A transaction that matched an order must not ALSO show up in the
    missing-on-machine pass."""
    report = build_reconciliation([_order(out_trade_no="TXN1")], [_payu_txn(txnid="TXN1")], "2026-08-01", "2026-08-31")
    assert len(report.rows) == 1
    assert report.rows[0].match_status == MATCHED


def test_multiple_orders_mixed_outcomes():
    orders = [
        _order(order_id="1", out_trade_no="A", order_money="100.00"),  # matches
        _order(order_id="2", out_trade_no="B", order_money="200.00"),  # mismatch
        _order(order_id="3", out_trade_no="C", payment_status="Have paid"),  # missing in payu
        _order(order_id="4", pay_type="Self-check repair", payment_status="Have paid", out_trade_no=""),  # excluded
    ]
    payu_txns = [
        _payu_txn(txnid="A", amount="100.00"),
        _payu_txn(txnid="B", amount="999.00"),
        _payu_txn(txnid="D", status="success"),  # orphaned on PayU side
    ]
    report = build_reconciliation(orders, payu_txns, "2026-08-01", "2026-08-31")

    assert report.matched_count == 1
    assert report.mismatch_count == 1
    assert report.missing_in_payu_count == 1
    assert report.missing_on_machine_count == 1
    assert len(report.rows) == 4  # the Self-check repair order never appears
