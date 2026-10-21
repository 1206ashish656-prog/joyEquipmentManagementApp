"""
Unit tests for services/refund_check.py's pure filtering logic -- no DB,
just OrderPaymentRecord/RefundCheck objects in and FailedTransactionRow
objects out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from db.models import OrderPaymentRecord, RefundCheck, UnrecordedPaymentCheck, User
from services.payu_client import PayURefund, PayUTransaction
from services.refund_check import (
    RefundDecision,
    append_trace,
    build_failed_transactions,
    build_unrecorded_payments,
    decide_refund_action,
    filter_by_payu_search,
    index_payu_transactions_by_mihpayid,
    index_payu_transactions_by_txnid,
    is_failed_transaction,
    is_unrecorded_payment,
    split_by_refund_status,
)


def _order(
    order_id="1", order_code="ORD1", device_app="NEXUS", order_date="2026-08-29",
    order_money="120.00", pay_type="UPI", payment_status="Have paid",
    order_status="Completed", delivery_status="Success", out_trade_no=None,
    created_at_target=None,
):
    return OrderPaymentRecord(
        order_id=order_id, order_code=order_code, device_app=device_app, order_date=order_date,
        order_money=Decimal(order_money), pay_type=pay_type, payment_status=payment_status,
        order_status=order_status, delivery_status=delivery_status, out_trade_no=out_trade_no,
        created_at_target=created_at_target or datetime.now(timezone.utc),
    )


def _payu_txn(txnid="TXN1", mihpayid="PAYU_ID_1", amount="120.00", status="captured", bank_ref_no=""):
    return PayUTransaction(txnid=txnid, mihpayid=mihpayid, amount=Decimal(amount), status=status, mode="UPI", addedon="", bank_ref_no=bank_ref_no, raw={})


def _payu_refund(refund_token="TOKEN1", request_id="REQ1", amount="120.00", status="success", bank_ref_no=""):
    return PayURefund(refund_token=refund_token, request_id=request_id, amount=Decimal(amount), status=status, created_at="2026-10-20 01:00:00", bank_ref_no=bank_ref_no, raw={})


def test_paid_and_fully_successful_order_is_not_a_failed_transaction():
    assert not is_failed_transaction(_order())


def test_paid_but_delivery_failed_is_a_failed_transaction():
    assert is_failed_transaction(_order(delivery_status="Failure"))


def test_paid_but_order_not_completed_is_a_failed_transaction():
    assert is_failed_transaction(_order(order_status="Cancel"))


def test_unpaid_order_with_bad_delivery_is_not_flagged():
    """No payment was ever collected -- a genuinely failed/abandoned
    checkout, not something to refund."""
    assert not is_failed_transaction(_order(payment_status="Non-payment", delivery_status="Failure"))


def test_non_upi_order_never_flagged_even_if_paid_and_failed():
    """"Self-check repair" orders never reach PayU -- nothing to refund
    regardless of status, per explicit request ("failed delivery despite
    receiving payment through upi")."""
    assert not is_failed_transaction(_order(pay_type="Self-check repair", delivery_status="Failure"))


def test_unknown_order_and_delivery_status_never_flagged():
    """Bug found live 2026-10-21: order_status/delivery_status default to
    "" for any row written before these columns existed (e.g. a
    historical row from an ALTER TABLE ADD COLUMN backfill) -- "" !=
    "Completed" is True, so without a guard every such row was wrongly
    flagged as a failed transaction, even though the machine's real
    status for it is simply unknown to this app, not actually failed."""
    assert not is_failed_transaction(_order(order_status="", delivery_status=""))


def test_unknown_order_status_alone_never_flagged():
    """Even a partially-known status (only one of the two fields empty)
    isn't trustworthy enough to flag -- both are needed to determine the
    Completed+Success state reliably."""
    assert not is_failed_transaction(_order(order_status="", delivery_status="Failure"))


def test_unknown_delivery_status_alone_never_flagged():
    assert not is_failed_transaction(_order(order_status="Cancel", delivery_status=""))


def test_genuinely_known_failed_status_still_flagged():
    """The guard must not swallow real failures -- only genuinely empty
    status fields are excluded."""
    assert is_failed_transaction(_order(order_status="Completed", delivery_status="Failure"))


def test_build_failed_transactions_excludes_successful_orders():
    rows = build_failed_transactions([_order()], {})
    assert rows == []


def test_build_failed_transactions_includes_failed_ones():
    rows = build_failed_transactions([_order(delivery_status="Failure")], {})
    assert len(rows) == 1
    assert rows[0].order_id == "1"
    assert rows[0].refund_requested is False
    assert rows[0].refund_requested_at is None
    assert rows[0].refund_requested_by is None


def test_build_failed_transactions_reflects_existing_refund_check():
    admin = User(id=7, name="Admin One", email="admin1@example.com", role="admin")
    requested_at = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    rc = RefundCheck(order_id="1", refund_requested=True, requested_at=requested_at)
    rc.requested_by = admin

    rows = build_failed_transactions([_order(delivery_status="Failure")], {"1": rc})

    assert len(rows) == 1
    assert rows[0].refund_requested is True
    assert rows[0].refund_requested_at == requested_at
    assert rows[0].refund_requested_by == "Admin One"


def test_build_failed_transactions_sorted_most_recent_date_and_time_first():
    rows = build_failed_transactions(
        [
            _order(order_id="1", order_date="2026-08-27", delivery_status="Failure",
                   created_at_target=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)),
            _order(order_id="2", order_date="2026-08-29", delivery_status="Failure",
                   created_at_target=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)),
            _order(order_id="3", order_date="2026-08-28", delivery_status="Failure",
                   created_at_target=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)),
        ],
        {},
    )
    assert [r.order_date for r in rows] == ["2026-08-29", "2026-08-28", "2026-08-27"]


def test_build_failed_transactions_sorted_by_time_within_same_date():
    """"Order by date and time" -- two orders on the same calendar date
    still sort by their actual time of day, not just tie-broken by
    order_id."""
    rows = build_failed_transactions(
        [
            _order(order_id="1", order_date="2026-08-29", delivery_status="Failure",
                   created_at_target=datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)),
            _order(order_id="2", order_date="2026-08-29", delivery_status="Failure",
                   created_at_target=datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)),
        ],
        {},
    )
    assert [r.order_id for r in rows] == ["2", "1"]


def test_split_by_refund_status_separates_refunded_from_non_refunded():
    """"Show only those orders for which refund is not triggered and
    hide orders that already received refund" -- split_by_refund_status()
    is the function behind the hide/unhide split on the page. Both
    output lists stay most-recent-first, since the input is already
    recency-sorted and partitioning is stable."""
    orders = [
        _order(order_id="1", order_code="ORD1", order_date="2026-08-27", delivery_status="Failure",
               created_at_target=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)),
        _order(order_id="2", order_code="ORD2", order_date="2026-08-29", delivery_status="Failure",
               created_at_target=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)),
        _order(order_id="3", order_code="ORD3", order_date="2026-08-28", delivery_status="Failure",
               created_at_target=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)),
    ]
    payu_refunds_by_txnid = {"ORD2": [_payu_refund(status="success")]}
    rows = build_failed_transactions(orders, {}, payu_refunds_by_txnid=payu_refunds_by_txnid)

    non_refunded, refunded = split_by_refund_status(rows)

    assert [r.order_id for r in non_refunded] == ["3", "1"]  # recency order preserved, refunded one excluded
    assert [r.order_id for r in refunded] == ["2"]
    assert all(r.payu_refund_id is None for r in non_refunded)
    assert all(r.payu_refund_id is not None for r in refunded)


def test_build_failed_transactions_matches_payu_by_out_trade_no():
    """out_trade_no is the correlation key here -- it's the same value
    PayU's API calls the internal transaction id (mihpayid), confirmed
    against the vendor's own 'external order id' linkage. NOT order_code
    (that's services/reconciliation.py's own, separate match)."""
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    payu_txns = [_payu_txn(mihpayid="PAYU_ID_1", status="dropped", bank_ref_no="REF123")]

    rows = build_failed_transactions([order], {}, payu_txns)

    assert rows[0].payu_id == "PAYU_ID_1"
    assert rows[0].payu_payment_status == "dropped"
    assert rows[0].payu_bank_ref_no == "REF123"


def test_build_failed_transactions_no_payu_match_leaves_fields_none():
    order = _order(delivery_status="Failure", out_trade_no="UNKNOWN_ID")
    rows = build_failed_transactions([order], {}, [_payu_txn(mihpayid="PAYU_ID_1")])

    assert rows[0].payu_id is None
    assert rows[0].payu_payment_status is None
    assert rows[0].payu_bank_ref_no is None


def test_build_failed_transactions_no_payu_data_passed_defaults_to_none():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    rows = build_failed_transactions([order], {})

    assert rows[0].payu_id is None
    assert rows[0].payu_payment_status is None
    assert rows[0].payu_bank_ref_no is None


def test_build_failed_transactions_order_code_never_used_for_payu_match():
    """Regression guard: order_code happening to equal a key in the PayU
    lookup must not accidentally match -- only out_trade_no does."""
    order = _order(order_code="TXN1", out_trade_no="SOME_OTHER_PAYU_ID", delivery_status="Failure")
    payu_txns = [_payu_txn(mihpayid="TXN1")]  # mihpayid "TXN1" happens to equal the order's order_code, not its out_trade_no

    rows = build_failed_transactions([order], {}, payu_txns)

    assert rows[0].payu_id is None


# --- duplicate mihpayid handling (regression: confirmed live 2026-10-20
# that PayU's real API returns more than one row for the same mihpayid
# for UPI payments, with the bank UTR only populated on some of them,
# in response-order that isn't stable between calls) ---

def test_index_payu_transactions_prefers_row_with_bank_ref_no():
    txns = [
        _payu_txn(mihpayid="ID1", bank_ref_no=""),
        _payu_txn(mihpayid="ID1", bank_ref_no="UTR123"),
    ]
    indexed = index_payu_transactions_by_mihpayid(txns)
    assert indexed["ID1"].bank_ref_no == "UTR123"


def test_index_payu_transactions_prefers_bank_ref_no_regardless_of_order():
    """The fix must not depend on which duplicate PayU happens to list
    first -- that order isn't stable between calls."""
    txns = [
        _payu_txn(mihpayid="ID1", bank_ref_no="UTR123"),
        _payu_txn(mihpayid="ID1", bank_ref_no=""),
    ]
    indexed = index_payu_transactions_by_mihpayid(txns)
    assert indexed["ID1"].bank_ref_no == "UTR123"


def test_index_payu_transactions_neither_duplicate_has_bank_ref_no():
    txns = [
        _payu_txn(mihpayid="ID1", bank_ref_no=""),
        _payu_txn(mihpayid="ID1", bank_ref_no=""),
    ]
    indexed = index_payu_transactions_by_mihpayid(txns)
    assert indexed["ID1"].bank_ref_no == ""


def test_build_failed_transactions_shows_bank_ref_no_regardless_of_duplicate_order():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    payu_txns = [
        _payu_txn(mihpayid="PAYU_ID_1", bank_ref_no=""),
        _payu_txn(mihpayid="PAYU_ID_1", bank_ref_no="UTR999"),
    ]
    rows = build_failed_transactions([order], {}, payu_txns)
    assert rows[0].payu_bank_ref_no == "UTR999"


# --- refund verification (payu_refund_id / payu_refund_status, looked
# up straight from PayU's getAllRefundsFromTxnIds, independent of our
# own refund_check "admin clicked the button" intent flag). Keyed by
# order_code/txnid -- NOT out_trade_no/mihpayid like the PayU ID/Status/
# Bank Ref No columns -- confirmed live 2026-10-20 that this specific
# PayU command uses the opposite id convention (see
# services/payu_client.py's GET_ALL_REFUNDS_COMMAND comment). The
# displayed id is refund_token (what PayU's dashboard calls "Refund
# ID"), not request_id (a different PayU id on the same refund row).

def test_build_failed_transactions_shows_refund_from_payu():
    order = _order(order_code="ORD1", delivery_status="Failure")
    refunds_by_txnid = {"ORD1": [_payu_refund(refund_token="TOKEN123", status="success")]}

    rows = build_failed_transactions([order], {}, [], refunds_by_txnid)

    assert rows[0].payu_refund_id == "TOKEN123"
    assert rows[0].payu_refund_status == "success"


def test_build_failed_transactions_refund_id_is_refund_token_not_request_id():
    """Regression guard: the dashboard's "Refund ID" is refund_token --
    request_id is a different PayU id that must never be shown as if it
    were the refund id."""
    order = _order(order_code="ORD1", delivery_status="Failure")
    refunds_by_txnid = {"ORD1": [_payu_refund(refund_token="TOKEN123", request_id="REQ999", status="success")]}

    rows = build_failed_transactions([order], {}, [], refunds_by_txnid)

    assert rows[0].payu_refund_id == "TOKEN123"
    assert rows[0].payu_refund_id != "REQ999"


def test_build_failed_transactions_no_refund_on_payu_leaves_fields_none():
    order = _order(order_code="ORD1", delivery_status="Failure")
    rows = build_failed_transactions([order], {}, [], {"ORD1": []})

    assert rows[0].payu_refund_id is None
    assert rows[0].payu_refund_status is None


def test_build_failed_transactions_refund_lookup_keyed_by_order_code_not_out_trade_no():
    """Regression guard: an order's out_trade_no happening to equal a
    key in the refunds dict must not accidentally match -- only
    order_code does, for this specific PayU command."""
    order = _order(order_code="ORD1", out_trade_no="SOME_PAYU_ID", delivery_status="Failure")
    refunds_by_txnid = {"SOME_PAYU_ID": [_payu_refund(refund_token="SHOULD_NOT_APPEAR")]}

    rows = build_failed_transactions([order], {}, [], refunds_by_txnid)

    assert rows[0].payu_refund_id is None


def test_build_failed_transactions_refund_requested_locally_does_not_imply_payu_refund():
    """Regression guard: our own refund_check 'intent' flag (an admin
    clicked Refund) must never be confused with an actual PayU refund --
    these are deliberately two separate columns/sources."""
    rc = RefundCheck(order_id="1", refund_requested=True, requested_at=datetime.now(timezone.utc))
    order = _order(order_id="1", order_code="ORD1", delivery_status="Failure")

    rows = build_failed_transactions([order], {"1": rc}, [], {})

    assert rows[0].refund_requested is True  # the local intent flag
    assert rows[0].payu_refund_id is None  # but PayU shows no actual refund


def test_pick_refund_prefers_success_over_other_statuses():
    order = _order(order_code="ORD1", delivery_status="Failure")
    refunds_by_txnid = {
        "ORD1": [_payu_refund(refund_token="TOKEN_FAILED", status="failure"), _payu_refund(refund_token="TOKEN_OK", status="success")],
    }

    rows = build_failed_transactions([order], {}, [], refunds_by_txnid)

    assert rows[0].payu_refund_id == "TOKEN_OK"
    assert rows[0].payu_refund_status == "success"


def test_pick_refund_prefers_in_progress_over_failure_when_no_success():
    order = _order(order_code="ORD1", delivery_status="Failure")
    refunds_by_txnid = {
        "ORD1": [_payu_refund(refund_token="TOKEN_FAILED", status="failure"), _payu_refund(refund_token="TOKEN_PROGRESS", status="in_progress")],
    }

    rows = build_failed_transactions([order], {}, [], refunds_by_txnid)

    assert rows[0].payu_refund_id == "TOKEN_PROGRESS"


def test_build_failed_transactions_mixed_batch():
    orders = [
        _order(order_id="1"),  # fully successful -- excluded
        _order(order_id="2", delivery_status="Failure"),
        _order(order_id="3", order_status="Cancel"),
        _order(order_id="4", payment_status="Non-payment", order_status="Cancel"),  # never paid -- excluded
    ]
    rows = build_failed_transactions(orders, {})
    assert {r.order_id for r in rows} == {"2", "3"}


# --- decide_refund_action (idempotency core for the POST /refund-check/{id}/refund handler) ---

def test_decide_refund_action_triggers_when_nothing_exists():
    assert decide_refund_action(existing=None, live_refund=None) == RefundDecision.TRIGGER


def test_decide_refund_action_locked_once_api_requested_at_is_set():
    """The idempotency lock: a prior call already got a definitive
    response from PayU -- a repeat click must be a true no-op."""
    rc = RefundCheck(order_id="1", refund_api_requested_at=datetime.now(timezone.utc))
    assert decide_refund_action(existing=rc, live_refund=None) == RefundDecision.ALREADY_LOCKED


def test_decide_refund_action_not_locked_when_only_intent_flag_is_set():
    """Clicking 'Refund' alone (refund_requested) must not lock anything
    by itself -- only a definitive PayU response (refund_api_requested_at)
    does. Covers the moment between an admin's click and the API call
    actually completing."""
    rc = RefundCheck(order_id="1", refund_requested=True, requested_at=datetime.now(timezone.utc), refund_api_requested_at=None)
    assert decide_refund_action(existing=rc, live_refund=None) == RefundDecision.TRIGGER


def test_decide_refund_action_backfills_when_payu_already_shows_a_refund():
    """The exact real-world case found this session: an admin refunded
    orders 322727/323113 directly on PayU's own dashboard, bypassing
    this app entirely. This app must never call initiate_refund() for
    an order PayU already shows as refunded."""
    live_refund = _payu_refund(refund_token="31386182688_506445f139", status="success")
    assert decide_refund_action(existing=None, live_refund=live_refund) == RefundDecision.BACKFILL_FROM_PAYU


def test_decide_refund_action_backfill_takes_priority_only_when_not_already_locked():
    """If we're already locked, stay locked -- don't re-backfill on
    every page load even though PayU's refund still shows up live."""
    rc = RefundCheck(order_id="1", refund_api_requested_at=datetime.now(timezone.utc))
    live_refund = _payu_refund(status="success")
    assert decide_refund_action(existing=rc, live_refund=live_refund) == RefundDecision.ALREADY_LOCKED


# --- append_trace (step-by-step audit log, per explicit request to
# "show which endpoints hit and the refund status") ---

def test_append_trace_to_empty_log():
    rc = RefundCheck(order_id="1")
    append_trace(rc, "first step", now=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc))
    assert rc.refund_trace == "[2026-10-20 12:00:00 UTC] first step"


def test_append_trace_accumulates_in_order():
    rc = RefundCheck(order_id="1")
    append_trace(rc, "step one", now=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc))
    append_trace(rc, "step two", now=datetime(2026, 10, 20, 12, 0, 5, tzinfo=timezone.utc))
    assert rc.refund_trace == (
        "[2026-10-20 12:00:00 UTC] step one\n"
        "[2026-10-20 12:00:05 UTC] step two"
    )


def test_build_failed_transactions_exposes_refund_trace():
    rc = RefundCheck(order_id="1")
    append_trace(rc, "did something", now=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc))
    order = _order(order_id="1", delivery_status="Failure")

    rows = build_failed_transactions([order], {"1": rc})

    assert rows[0].refund_trace == "[2026-10-20 12:00:00 UTC] did something"


# --- unrecorded payments (the inverse: PayU shows paid, machine says
# it isn't -- per explicit request, "received payment as per payU but
# didn't receive payment by the machine") ---

def test_is_unrecorded_payment_true_when_payu_captured_but_machine_says_non_payment():
    order = _order(payment_status="Non-payment", order_code="TXN1")
    payu_txn = _payu_txn(txnid="TXN1", status="captured")
    assert is_unrecorded_payment(order, payu_txn)


def test_is_unrecorded_payment_false_when_machine_also_says_paid():
    """If the machine DOES say 'Have paid', this is a normal matched
    order (services/reconciliation.py's territory), not an unrecorded
    payment -- these two categories must never overlap."""
    order = _order(payment_status="Have paid", order_code="TXN1")
    payu_txn = _payu_txn(txnid="TXN1", status="captured")
    assert not is_unrecorded_payment(order, payu_txn)


def test_is_unrecorded_payment_false_when_payu_also_shows_no_payment():
    """Both sides agree nothing was collected -- a genuinely abandoned
    checkout, not a discrepancy worth reviewing."""
    order = _order(payment_status="Non-payment", order_code="TXN1")
    payu_txn = _payu_txn(txnid="TXN1", status="dropped")
    assert not is_unrecorded_payment(order, payu_txn)


def test_is_unrecorded_payment_false_when_no_matching_payu_transaction():
    order = _order(payment_status="Non-payment", order_code="TXN1")
    assert not is_unrecorded_payment(order, None)


def test_is_unrecorded_payment_false_for_non_upi():
    """"Self-check repair" orders never reach PayU -- can't have a
    PayU-side payment to be unrecorded in the first place."""
    order = _order(payment_status="Non-payment", order_code="TXN1", pay_type="Self-check repair")
    payu_txn = _payu_txn(txnid="TXN1", status="captured")
    assert not is_unrecorded_payment(order, payu_txn)


def test_index_payu_transactions_by_txnid_dedupes_like_mihpayid_version():
    txns = [
        _payu_txn(txnid="TXN1", mihpayid="A", bank_ref_no=""),
        _payu_txn(txnid="TXN1", mihpayid="B", bank_ref_no="UTR123"),
    ]
    indexed = index_payu_transactions_by_txnid(txns)
    assert indexed["TXN1"].bank_ref_no == "UTR123"


def test_build_unrecorded_payments_includes_matches_and_excludes_non_matches():
    orders = [
        _order(order_id="1", order_code="TXN1", payment_status="Non-payment"),  # unrecorded -- included
        _order(order_id="2", order_code="TXN2", payment_status="Have paid"),  # normal paid order -- excluded
        _order(order_id="3", order_code="TXN3", payment_status="Non-payment"),  # genuinely unpaid on both sides -- excluded
    ]
    payu_txns = [
        _payu_txn(txnid="TXN1", mihpayid="PAYUID1", status="captured", amount="135.00"),
        _payu_txn(txnid="TXN2", mihpayid="PAYUID2", status="captured", amount="120.00"),
        _payu_txn(txnid="TXN3", mihpayid="PAYUID3", status="dropped"),
    ]

    rows = build_unrecorded_payments(orders, {}, payu_txns)

    assert {r.order_id for r in rows} == {"1"}
    assert rows[0].payu_id == "PAYUID1"
    assert rows[0].payu_payment_status == "captured"
    assert rows[0].payu_amount == Decimal("135.00")


def test_build_unrecorded_payments_shows_refund_status_keyed_by_order_code():
    order = _order(order_id="1", order_code="TXN1", payment_status="Non-payment")
    payu_txns = [_payu_txn(txnid="TXN1", mihpayid="PAYUID1", status="captured")]
    payu_refunds = {"TXN1": [_payu_refund(refund_token="REFUND_TOKEN", status="success")]}

    rows = build_unrecorded_payments([order], {}, payu_txns, payu_refunds)

    assert rows[0].payu_refund_id == "REFUND_TOKEN"
    assert rows[0].payu_refund_status == "success"


def test_build_unrecorded_payments_reuses_refund_check_style_fields():
    rc = UnrecordedPaymentCheck(order_id="1", refund_requested=True, requested_at=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    append_trace(rc, "did something", now=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc))
    order = _order(order_id="1", order_code="TXN1", payment_status="Non-payment")
    payu_txns = [_payu_txn(txnid="TXN1", status="captured")]

    rows = build_unrecorded_payments([order], {"1": rc}, payu_txns)

    assert rows[0].refund_requested is True
    assert rows[0].refund_trace == "[2026-10-20 12:00:00 UTC] did something"


def test_split_by_refund_status_separates_refunded_unrecorded_payments():
    orders = [
        _order(order_id="1", order_code="TXN1", payment_status="Non-payment",
               created_at_target=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)),
        _order(order_id="2", order_code="TXN2", payment_status="Non-payment",
               created_at_target=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)),
    ]
    payu_txns = [
        _payu_txn(txnid="TXN1", mihpayid="PAYUID1", status="captured"),
        _payu_txn(txnid="TXN2", mihpayid="PAYUID2", status="captured"),
    ]
    payu_refunds_by_txnid = {"TXN2": [_payu_refund(status="success")]}

    rows = build_unrecorded_payments(orders, {}, payu_txns, payu_refunds_by_txnid)
    non_refunded, refunded = split_by_refund_status(rows)

    assert [r.order_id for r in non_refunded] == ["1"]
    assert [r.order_id for r in refunded] == ["2"]


def test_build_unrecorded_payments_never_overlaps_with_failed_transactions():
    """A sanity check that the two pipelines are genuinely mutually
    exclusive for the same order set -- an order is never both."""
    orders = [
        _order(order_id="1", order_code="TXN1", payment_status="Have paid", delivery_status="Failure"),  # failed transaction
        _order(order_id="2", order_code="TXN2", payment_status="Non-payment"),  # unrecorded payment
    ]
    payu_txns = [_payu_txn(txnid="TXN2", mihpayid="PAYUID2", status="captured")]

    failed_ids = {r.order_id for r in build_failed_transactions(orders, {})}
    unrecorded_ids = {r.order_id for r in build_unrecorded_payments(orders, {}, payu_txns)}

    assert failed_ids == {"1"}
    assert unrecorded_ids == {"2"}
    assert not (failed_ids & unrecorded_ids)


def test_filter_by_payu_search_matches_payu_id():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    rows = build_failed_transactions([order], {}, [_payu_txn(mihpayid="PAYU_ID_1", bank_ref_no="REF123")])
    assert filter_by_payu_search(rows, "payu_id_1") == rows  # case-insensitive


def test_filter_by_payu_search_matches_bank_ref_no():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    rows = build_failed_transactions([order], {}, [_payu_txn(mihpayid="PAYU_ID_1", bank_ref_no="REF123")])
    assert filter_by_payu_search(rows, "ref123") == rows


def test_filter_by_payu_search_excludes_non_matches():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    rows = build_failed_transactions([order], {}, [_payu_txn(mihpayid="PAYU_ID_1", bank_ref_no="REF123")])
    assert filter_by_payu_search(rows, "nothing-matches-this") == []


def test_filter_by_payu_search_substring_match():
    order = _order(delivery_status="Failure", out_trade_no="PAYU_ID_1")
    rows = build_failed_transactions([order], {}, [_payu_txn(mihpayid="PAYU_ID_1", bank_ref_no="629304963204")])
    assert filter_by_payu_search(rows, "9304963") == rows


def test_filter_by_payu_search_empty_or_none_returns_all_rows():
    order = _order(delivery_status="Failure")
    rows = build_failed_transactions([order], {})
    assert filter_by_payu_search(rows, None) == rows
    assert filter_by_payu_search(rows, "   ") == rows


def test_filter_by_payu_search_handles_rows_with_no_payu_data():
    """An order with no PayU match at all (payu_id/payu_bank_ref_no both
    None) must not raise -- it just never matches a real search term."""
    order = _order(delivery_status="Failure")
    rows = build_failed_transactions([order], {})
    assert filter_by_payu_search(rows, "anything") == []
