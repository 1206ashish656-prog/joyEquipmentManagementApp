"""
Failed-transaction detection for the refund-check feature
(backend/api/refund_check.py): a machine order where payment was
completed (OrderPaymentRecord.payment_status == "Have paid") but the
order itself didn't fully succeed -- either order_status isn't
"Completed" or delivery_status isn't "Success". This is deliberately
the inverse of orders/selectors.py's SUMMARY_FILTER, which treats
exactly that (Completed + Success) as "qualifying".

Pure filtering/shaping logic (no I/O) -- backend/api/refund_check.py
does the DB query + PayU API call and passes the results in, same split
as services/reconciliation.py.

PayU cross-reference (added 2026-10-20, per explicit request): the
vendor's own Order Information dashboard links each order out to PayU
via what it calls the "external order id" -- confirmed by the account
owner that this is the same value as PayU's own internal transaction
id. That's OrderPaymentRecord.out_trade_no on our side (confirmed
separately against a real PayU account -- see
services/reconciliation.py's module docstring), surfaced from PayU's
API as PayUTransaction.mihpayid. So each failed-transaction row here is
matched to a PayU transaction via out_trade_no == mihpayid, NOT
order_code == txnid (that pair is services/reconciliation.py's own,
unrelated match).

Duplicate mihpayid handling (confirmed live 2026-10-20 against the real
account): get_Transaction_Details sometimes returns MORE THAN ONE row
for the same mihpayid/"id" -- observed for UPI payments where the
bank's UTR (bank_ref_no) is attached to the record asynchronously, some
time after the initial "captured" status. Both rows carry the same
mihpayid/status but different bank_ref_no (one populated earlier,
possibly null; a later one with a different/later-arriving value) --
and which row PayU's API lists first isn't stable between calls, so
naively keying a dict by mihpayid ("last one wins") non-deterministically
drops the bank_ref_no about half the time. _merge_payu_duplicate()
below always prefers whichever duplicate actually has bank_ref_no
populated, so a UTR that exists on ANY of the duplicate rows is shown.

Refund verification (added 2026-10-20, per explicit request): "refund
id" is deliberately NOT read from our own refund_check table (that only
records an admin's intent to refund -- see backend/api/refund_check.py,
and this feature still doesn't call any vendor refund API). It's looked
up straight from PayU via PayUClient.get_refunds_for_transaction() --
matched via order_code/txnid, NOT out_trade_no/mihpayid (see
services/payu_client.py's GET_ALL_REFUNDS_COMMAND comment for how that
was confirmed -- this API's own id convention is the OPPOSITE of
get_Transaction_Details's, despite being the same PayU account/command
family) -- so the column only ever shows a refund PayU itself confirms
actually happened. The displayed id is PayURefund.refund_token, which
is what PayU's own dashboard calls "Refund ID" (confirmed by matching a
real refunded transaction's dashboard screenshot against this API's
response for it) -- NOT PayURefund.request_id, a different PayU id on
the same row.

Refund-trigger pipeline (added 2026-10-20, per explicit request):
decide_refund_action() is the pure decision core behind
backend/api/refund_check.py's POST route, kept separate (and separately
tested) for the same reason _pick_refund() is -- it's the one piece of
real business logic (idempotency: never fire cancel_refund_transaction
twice for the same order, and never fire it at all if PayU already
shows a refund made outside this app -- confirmed this has already
happened for two real orders, 322727 and 323113, refunded directly on
PayU's own dashboard) in a flow that's otherwise I/O. The actual
PayU call (services/payu_client.py's initiate_refund()) was proven
against TEST/sandbox credentials first, then activated against
PRODUCTION credentials for real orders per explicit instruction
(2026-10-20, "route the actual refund call at the production PayU
account") -- backend/api/refund_check.py resolves which account per
order (production for a real one, test/sandbox only for a dormant
TEST_DEVICE_APP dummy order) and passes the same Settings to both the
pre-trigger lookup and initiate_refund(), so this function itself has
no account-specific logic to change.

Unrecorded payments (added 2026-10-20, per explicit request): the
INVERSE problem from is_failed_transaction() -- PayU shows the payment
as genuinely collected (status in PAYU_SUCCESS_STATUSES), but the
machine's own payment_status says it wasn't ("received payment as per
payU but didn't receive payment by the machine"). is_unrecorded_payment()/
build_unrecorded_payments() match via order_code == PayUTransaction.txnid
-- services/reconciliation.py's own key, NOT out_trade_no/mihpayid like
the failed-transaction functions above -- since that's the pair
confirmed live for matching an order to its PayU record at all (the
mihpayid-based match only makes sense once you already know which
PayU transaction belongs to the order, which for an "unrecorded"
order is exactly what's in question). Reuses decide_refund_action()/
append_trace() as-is (both are duck-typed against any object shaped
like RefundCheck, so UnrecordedPaymentCheck works without
modification) -- "functionality for these orders should be similar to"
the failed-transaction pipeline, per explicit request.

PayU/bank-ref search (added 2026-10-20, per explicit request):
filter_by_payu_search() is the "search by PayU ID and bank ref id"
request -- a single needle matched (case-insensitive, substring)
against either payu_id or payu_bank_ref_no, since both tables expose
both columns and a single search box covering either is simpler for an
admin than two separate inputs for what's really one lookup ("find
this transaction").

Hide-refunded split (added 2026-10-21, per explicit request: "the data
displayed looks clumsy... show only those orders for which refund is
not triggered and hide orders that already received refund"):
build_failed_transactions()/build_unrecorded_payments() sort rows by
most-recent-first (created_at_target -- date AND time, not just the
date string) and nothing else; split_by_refund_status() then
partitions that single recency-ordered list into (non_refunded,
refunded) -- since the partition is stable, each resulting list stays
recency-ordered on its own, with no separate sort needed per group.
backend/api/refund_check.py renders non_refunded directly and
refunded inside a collapsible section an admin can open to "unhide"
it ("allow user to unhide refunded transactions") -- per the same
request, a search still matches against BOTH groups
(filter_by_payu_search() is applied to each independently), so a
refunded order is still findable by its PayU ID/bank ref even while
collapsed.

Unknown-status bug, found and fixed in production 2026-10-21:
is_failed_transaction() now requires order_status AND delivery_status
to both be non-empty before considering an order at all. Without this
guard, any OrderPaymentRecord row written before those two columns
existed (they default to "" -- see db/models.py, and the production
migration that added them via ALTER TABLE ... DEFAULT '' left every
pre-existing row with that default rather than a real value) got
wrongly flagged: "" != COMPLETED_ORDER_STATUS evaluates True, so an
empty/unknown status looked identical to a genuine failure. Confirmed
live: this flooded the Failed Transactions table with historical
"Have paid" orders that have nothing actually wrong with them -- the
real fix for the underlying data is re-syncing those dates (orders.backfill
--force, which calls the current save_order_payment_records() and
populates the real values), but the code-level guard is the one that
actually matters going forward -- it protects against this exact class
of gap for ANY column added the same no-Alembic way in the future, not
just this one.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from db.models import OrderPaymentRecord, RefundCheck, UnrecordedPaymentCheck
from services.payu_client import PayURefund, PayUTransaction

PAID_STATUS = "Have paid"  # OrderPaymentRecord.payment_status meaning "machine recorded this as paid"
COMPLETED_ORDER_STATUS = "Completed"
SUCCESS_DELIVERY_STATUS = "Success"
UPI_PAY_TYPE = "UPI"  # only UPI orders ever reach PayU -- see services/reconciliation.py
# Mirrors services/reconciliation.py's own PAYU_SUCCESS_STATUSES constant
# (not imported from there to avoid a cross-module coupling for one
# literal) -- PayU's own "this payment genuinely happened" statuses.
PAYU_SUCCESS_STATUSES = {"success", "captured"}


@dataclass
class FailedTransactionRow:
    order_id: str
    order_code: str
    device_app: str
    order_date: str
    order_money: Decimal
    pay_type: str
    payment_status: str
    order_status: str
    delivery_status: str
    goods_name: str
    cup_num: int
    created_at_target: datetime  # order's own createtime -- the "time" half of the date+time sort, see _sort_by_recency()
    paid_at: datetime | None
    refund_requested: bool
    refund_requested_at: datetime | None
    refund_requested_by: str | None  # requesting admin's display name
    payu_id: str | None  # PayUTransaction.mihpayid, matched via out_trade_no -- None if not found/not looked up
    payu_payment_status: str | None  # PayUTransaction.status, e.g. "captured", "dropped" -- None likewise
    payu_bank_ref_no: str | None  # PayUTransaction.bank_ref_no -- None likewise
    payu_refund_id: str | None  # PayURefund.refund_token -- what PayU's own dashboard calls "Refund ID" -- None if no refund exists on PayU's side yet
    payu_refund_status: str | None  # PayURefund.status, e.g. "success", "queued" -- None likewise
    refund_api_requested_at: datetime | None  # when we actually called cancel_refund_transaction -- None if never (also the idempotency lock, see decide_refund_action())
    refund_api_error: str | None  # PayU's own rejection message for OUR trigger attempt, if any
    refund_mode: str | None  # "production" (real orders) | "test" (TEST_DEVICE_APP dummy orders only) -- which PayU account OUR trigger attempt used
    refund_trace: str | None  # timestamped step-by-step log of every PayU call made for this order -- see RefundCheck.refund_trace


def is_failed_transaction(order: OrderPaymentRecord) -> bool:
    """Payment completed via UPI, but the order/delivery itself didn't.
    Restricted to pay_type == "UPI" (added 2026-10-20, per explicit
    request: "failed delivery despite receiving payment through upi") --
    the only other pay_type ever seen, "Self-check repair", is a free/
    maintenance category that never reaches PayU, so it can never have
    anything to refund regardless of its order/delivery status.

    Requires order_status/delivery_status to actually be populated
    (bug found live 2026-10-21): these two columns default to "" for
    any OrderPaymentRecord row written before they existed (e.g. a
    historical row from an ALTER TABLE-added-column backfill, or any
    future column added the same way) -- "" != COMPLETED_ORDER_STATUS
    is True, so without this guard every such row was wrongly flagged
    as a failed transaction (confirmed live: flooded the table with
    historical "Have paid" orders that have nothing wrong with them,
    the machine's own status for them is just unknown to this app).
    Unknown status must mean "don't know," never "definitely failed.\""""
    if not order.order_status or not order.delivery_status:
        return False
    return (
        order.pay_type == UPI_PAY_TYPE
        and order.payment_status == PAID_STATUS
        and (order.order_status != COMPLETED_ORDER_STATUS or order.delivery_status != SUCCESS_DELIVERY_STATUS)
    )


def _merge_payu_duplicate(current: PayUTransaction, candidate: PayUTransaction) -> PayUTransaction:
    """Picks which of two PayU rows sharing the same mihpayid to keep --
    whichever has bank_ref_no populated, preferring `current` (the
    earlier-seen one) when both or neither do, so the result never
    depends on PayU's own (observed to be unstable) response order."""
    if not current.bank_ref_no and candidate.bank_ref_no:
        return candidate
    return current


def index_payu_transactions_by_mihpayid(
    payu_transactions: list[PayUTransaction],
) -> dict[str, PayUTransaction]:
    by_mihpayid: dict[str, PayUTransaction] = {}
    for t in payu_transactions:
        if not t.mihpayid:
            continue
        existing = by_mihpayid.get(t.mihpayid)
        by_mihpayid[t.mihpayid] = t if existing is None else _merge_payu_duplicate(existing, t)
    return by_mihpayid


_REFUND_STATUS_PRIORITY = ("success", "in_progress", "in progress", "queued", "requested", "failure")


def _pick_refund(refunds: list[PayURefund]) -> PayURefund | None:
    """When PayU has more than one refund action on record for a
    transaction (e.g. a failed attempt retried, or a genuine partial
    refund), prefer the most conclusive one to surface -- a successful
    refund outranks one still in flight, which outranks a failed
    attempt. Falls back to the first one seen for any status PayU
    returns that isn't in the known list, rather than hiding it."""
    if not refunds:
        return None
    by_status = {r.status: r for r in refunds}
    for status in _REFUND_STATUS_PRIORITY:
        if status in by_status:
            return by_status[status]
    return refunds[0]


class RefundDecision(enum.Enum):
    ALREADY_LOCKED = "already_locked"  # we already got a definitive PayU response for this order before -- no-op
    BACKFILL_FROM_PAYU = "backfill_from_payu"  # PayU already shows a refund we never triggered -- record it, don't call PayU again
    TRIGGER = "trigger"  # no lock, no existing PayU refund -- actually call initiate_refund()


def decide_refund_action(existing: RefundCheck | None, live_refund: PayURefund | None) -> RefundDecision:
    """The one real decision backend/api/refund_check.py's POST route
    needs to make, kept pure/testable (DB row + already-fetched live
    PayU lookup in, decision out -- no I/O here).

    Idempotency ("if refund request is already sent... ignore in the
    backend", per explicit request): `existing.refund_api_requested_at`
    is set only once a DEFINITIVE response -- success or a clean
    PayU-side rejection -- came back from a previous call. A transport
    failure before any PayU response leaves it unset, so a genuine
    network hiccup can still be retried; once PayU has actually
    processed a request for this order, it never will be again.

    BACKFILL_FROM_PAYU exists because an admin can refund directly on
    PayU's own dashboard, bypassing this app entirely -- confirmed this
    already happened for two real orders (322727, 323113) earlier this
    session. If PayU's own live lookup already shows a refund, this app
    must never call initiate_refund() for it (that would be a genuine
    duplicate refund attempt) -- it just needs to record what PayU
    already confirmed.
    """
    if existing is not None and existing.refund_api_requested_at is not None:
        return RefundDecision.ALREADY_LOCKED
    if live_refund is not None:
        return RefundDecision.BACKFILL_FROM_PAYU
    return RefundDecision.TRIGGER


def append_trace(rc: RefundCheck, line: str, now: datetime | None = None) -> None:
    """Appends one timestamped line to RefundCheck.refund_trace --
    shared by backend/api/refund_check.py's GET (status-refresh steps)
    and POST (idempotency-check/trigger steps) handlers, so every PayU
    call made for an order ends up in one readable log, in the order
    they actually happened, per explicit request to show "which
    endpoints hit and the refund status"."""
    now = now or datetime.now(timezone.utc)
    stamped = f"[{now.strftime('%Y-%m-%d %H:%M:%S')} UTC] {line}"
    rc.refund_trace = f"{rc.refund_trace}\n{stamped}" if rc.refund_trace else stamped


def _sort_by_recency(rows: list) -> list:
    """Most-recent-first by the order's own created_at_target (date AND
    time, not just the date string). Works for either row type
    (duck-typed: both have created_at_target). See split_by_refund_status()
    for how this feeds the hide-refunded split."""
    rows.sort(key=lambda r: r.created_at_target, reverse=True)
    return rows


def split_by_refund_status(rows: list) -> tuple[list, list]:
    """Partitions an already-recency-sorted row list into
    (non_refunded, refunded), per explicit request ("show only those
    orders for which refund is not triggered and hide orders that
    already received refund"). A plain partition rather than a sort --
    since the input is already most-recent-first and partitioning is
    stable, each output list stays recency-ordered on its own with no
    further sorting needed. Works for either row type (duck-typed: both
    have payu_refund_id)."""
    non_refunded = [r for r in rows if r.payu_refund_id is None]
    refunded = [r for r in rows if r.payu_refund_id is not None]
    return non_refunded, refunded


def filter_by_payu_search(rows: list, search: str | None) -> list:
    """"Search option... by payU id and bank ref id" -- a single search
    box (not two separate inputs) matched case-insensitively as a
    substring against either payu_id or payu_bank_ref_no, since an admin
    searching for a specific transaction may have either value in hand
    and both tables expose both columns. Works for either row type
    (duck-typed, same as split_by_refund_status() above). Empty/
    whitespace-only search is treated as no search. Applied to the
    non-refunded AND the refunded (hidden-by-default) list independently
    by the caller, per explicit request that hidden/refunded orders stay
    "searchable as well from the same search box"."""
    needle = (search or "").strip().lower()
    if not needle:
        return rows
    return [
        r for r in rows
        if needle in (r.payu_id or "").lower() or needle in (r.payu_bank_ref_no or "").lower()
    ]


def build_failed_transactions(
    orders: list[OrderPaymentRecord],
    refund_checks_by_order_id: dict[str, RefundCheck],
    payu_transactions: list[PayUTransaction] | None = None,
    payu_refunds_by_txnid: dict[str, list[PayURefund]] | None = None,
) -> list[FailedTransactionRow]:
    payu_txns_by_mihpayid = index_payu_transactions_by_mihpayid(payu_transactions or [])
    payu_refunds_by_txnid = payu_refunds_by_txnid or {}
    rows: list[FailedTransactionRow] = []
    for order in orders:
        if not is_failed_transaction(order):
            continue
        rc = refund_checks_by_order_id.get(order.order_id)
        payu_txn = payu_txns_by_mihpayid.get(order.out_trade_no) if order.out_trade_no else None
        refund = _pick_refund(payu_refunds_by_txnid.get(order.order_code, [])) if order.order_code else None
        rows.append(FailedTransactionRow(
            order_id=order.order_id,
            order_code=order.order_code,
            device_app=order.device_app,
            order_date=order.order_date,
            order_money=order.order_money,
            pay_type=order.pay_type,
            payment_status=order.payment_status,
            order_status=order.order_status,
            delivery_status=order.delivery_status,
            goods_name=order.goods_name,
            cup_num=order.cup_num,
            created_at_target=order.created_at_target,
            paid_at=order.paid_at,
            refund_requested=bool(rc and rc.refund_requested),
            refund_requested_at=rc.requested_at if rc else None,
            refund_requested_by=rc.requested_by.name if rc and rc.requested_by else None,
            payu_id=payu_txn.mihpayid if payu_txn else None,
            payu_payment_status=payu_txn.status if payu_txn else None,
            payu_bank_ref_no=payu_txn.bank_ref_no if payu_txn else None,
            payu_refund_id=refund.refund_token if refund else None,
            payu_refund_status=refund.status if refund else None,
            refund_api_requested_at=rc.refund_api_requested_at if rc else None,
            refund_api_error=rc.refund_api_error if rc else None,
            refund_mode=rc.refund_mode if rc else None,
            refund_trace=rc.refund_trace if rc else None,
        ))

    return _sort_by_recency(rows)


@dataclass
class UnrecordedPaymentRow:
    order_id: str
    order_code: str
    device_app: str
    order_date: str
    order_money: Decimal
    pay_type: str
    payment_status: str  # the machine's own status, e.g. "Non-payment" -- the whole point of this row
    order_status: str
    delivery_status: str
    goods_name: str
    cup_num: int
    created_at_target: datetime  # order's own createtime -- the "time" half of the date+time sort, see _sort_by_recency()
    payu_id: str | None  # PayUTransaction.mihpayid of the matched transaction -- None if somehow not found
    payu_payment_status: str | None  # PayUTransaction.status, e.g. "captured" -- what makes this row exist at all
    payu_amount: Decimal | None  # what PayU actually shows as collected, for comparison against order_money
    payu_bank_ref_no: str | None
    refund_requested: bool
    refund_requested_at: datetime | None
    refund_requested_by: str | None
    payu_refund_id: str | None
    payu_refund_status: str | None
    refund_api_requested_at: datetime | None
    refund_api_error: str | None
    refund_mode: str | None
    refund_trace: str | None


def index_payu_transactions_by_txnid(
    payu_transactions: list[PayUTransaction],
) -> dict[str, PayUTransaction]:
    """Same duplicate-handling as index_payu_transactions_by_mihpayid()
    (prefer whichever duplicate has bank_ref_no), just keyed by txnid --
    the match key for this feature, not mihpayid."""
    by_txnid: dict[str, PayUTransaction] = {}
    for t in payu_transactions:
        if not t.txnid:
            continue
        existing = by_txnid.get(t.txnid)
        by_txnid[t.txnid] = t if existing is None else _merge_payu_duplicate(existing, t)
    return by_txnid


def is_unrecorded_payment(order: OrderPaymentRecord, payu_txn: PayUTransaction | None) -> bool:
    """PayU shows this UPI order as genuinely paid; the machine's own
    record of it says otherwise. See module docstring for the match key
    (order_code == txnid) and why it differs from is_failed_transaction()'s."""
    return (
        order.pay_type == UPI_PAY_TYPE
        and order.payment_status != PAID_STATUS
        and payu_txn is not None
        and payu_txn.status in PAYU_SUCCESS_STATUSES
    )


def build_unrecorded_payments(
    orders: list[OrderPaymentRecord],
    checks_by_order_id: dict[str, UnrecordedPaymentCheck],
    payu_transactions: list[PayUTransaction] | None = None,
    payu_refunds_by_txnid: dict[str, list[PayURefund]] | None = None,
) -> list[UnrecordedPaymentRow]:
    payu_txns_by_txnid = index_payu_transactions_by_txnid(payu_transactions or [])
    payu_refunds_by_txnid = payu_refunds_by_txnid or {}
    rows: list[UnrecordedPaymentRow] = []
    for order in orders:
        payu_txn = payu_txns_by_txnid.get(order.order_code) if order.order_code else None
        if not is_unrecorded_payment(order, payu_txn):
            continue
        rc = checks_by_order_id.get(order.order_id)
        refund = _pick_refund(payu_refunds_by_txnid.get(order.order_code, [])) if order.order_code else None
        rows.append(UnrecordedPaymentRow(
            order_id=order.order_id,
            order_code=order.order_code,
            device_app=order.device_app,
            order_date=order.order_date,
            order_money=order.order_money,
            pay_type=order.pay_type,
            payment_status=order.payment_status,
            order_status=order.order_status,
            delivery_status=order.delivery_status,
            goods_name=order.goods_name,
            cup_num=order.cup_num,
            created_at_target=order.created_at_target,
            payu_id=payu_txn.mihpayid if payu_txn else None,
            payu_payment_status=payu_txn.status if payu_txn else None,
            payu_amount=payu_txn.amount if payu_txn else None,
            payu_bank_ref_no=payu_txn.bank_ref_no if payu_txn else None,
            refund_requested=bool(rc and rc.refund_requested),
            refund_requested_at=rc.requested_at if rc else None,
            refund_requested_by=rc.requested_by.name if rc and rc.requested_by else None,
            payu_refund_id=refund.refund_token if refund else None,
            payu_refund_status=refund.status if refund else None,
            refund_api_requested_at=rc.refund_api_requested_at if rc else None,
            refund_api_error=rc.refund_api_error if rc else None,
            refund_mode=rc.refund_mode if rc else None,
            refund_trace=rc.refund_trace if rc else None,
        ))

    return _sort_by_recency(rows)
