"""
Failed-transaction / refund-check endpoint (admin-only, /refund-check):
surfaces machine orders where payment was completed via UPI but the
order itself didn't fully succeed (order_status != "Completed" and/or
delivery_status != "Success" -- see services/refund_check.py), and lets
an admin trigger a PayU refund against them. Also surfaces the INVERSE
problem (added 2026-10-20, per explicit request) in a second table on
the same page: orders PayU shows as genuinely paid but the machine's
own payment_status says otherwise -- see services/refund_check.py's
is_unrecorded_payment().

Realtime window (2026-10-20, per explicit request): juice dispensing
takes ~120s, so an order younger than that can show "Failure" as a
still-in-progress state, not a genuine one -- MIN_ORDER_AGE_SECONDS
excludes anything younger than 150s (120s + buffer) regardless of its
current status. orders/realtime_worker.py's refresh interval was
tightened from 300s to 150s to match (see its own docstring) -- without
that, this threshold would be theoretical, since an order could sit
un-synced for minutes before even reaching OrderPaymentRecord.

Production mode, read-only display (2026-10-20, per explicit request:
"move from test mode to production payU app"): the read-only
cross-reference (PayU ID/Status/Bank Ref/Refund ID/Status columns) has
always defaulted to Settings.payu_merchant_key/salt -- the real
production PayU account -- for any order not tagged TEST_DEVICE_APP,
so real orders already show live production PayU data.

Refund-trigger activation, PRODUCTION account, real money (2026-10-20,
per explicit instruction: "Activate the functionality in the current
setup keeping the constraint on refund authorization intact" ->
clarified, when asked which account, to "route the actual refund call
at the production PayU account (real refunds, real money)"):
REFUND_TRIGGER_ENABLED is now True. Both POST routes resolve the SAME
account for the trigger call as they already did for the pre-trigger
lookup (`_payu_settings_for()`'s `lookup_settings`/`lookup_account`) --
production for any real order, test/sandbox ONLY for a dormant
TEST_DEVICE_APP dummy order (see below). This means a real admin click
on a real order now sends a genuine `cancel_refund_transaction` to
PayU's production account -- verified end-to-end against TEST/sandbox
credentials earlier this session (see services/payu_client.py's
CANCEL_REFUND_COMMAND comment for that verification trail); the hash
formula and request shape are identical against production, only the
account differs.

"Keeping the constraint on refund authorization intact" means exactly
what it already enforced before this activation, unchanged by it: a
refund is only ever sent from inside a `POST /refund-check/.../refund`
request, gated by `require_admin` (an authenticated admin, not any
other role) and triggered client-side only via the button's `confirm()`
dialog (backend/templates/refund_check.html) -- there is no scheduled
job, worker cycle, or GET-triggered path anywhere in this codebase that
can call `initiate_refund()`; the combined worker / realtime_worker
only ever write OrderPaymentRecord rows, never touch RefundCheck/
UnrecordedPaymentCheck, and refund_check_page() below (the GET handler)
only ever reads/display-caches PayU's existing status, never triggers.
Flipping REFUND_TRIGGER_ENABLED back to False (e.g. to pause the whole
pipeline again) remains a one-line change and reverts to the fully
read-only posture with zero PayU calls per POST, same as before this
activation.

Idempotency (services/refund_check.py's decide_refund_action(),
unchanged by this activation): a repeat click/POST for an order that
already has a definitive PayU response locked in is a silent no-op,
and an order PayU already shows as refunded (e.g. refunded directly on
PayU's own dashboard, bypassing this app -- confirmed this has already
happened for two real orders) is backfilled rather than refunded a
second time. This is the mechanism that keeps "never auto-refund an
order the admin hasn't actually clicked" true even now that the
account is real.

TEST_DEVICE_APP / _payu_settings_for() remain in place (dormant, no
behavioral cost when no such rows exist) from the earlier test-mode
verification phase -- the dummy orders themselves were removed once
that verification was complete. Kept specifically so the SAME dormant
path still resolves to test/sandbox credentials for a
TEST_DEVICE_APP-tagged order's trigger call too, in case TEST-mode
verification is ever needed again without risking a real order's
production credentials.

Search (2026-10-20, per explicit request): a single `?search=` query
param (one search box in the template, shared by both tables) filters
rows to those whose PayU ID or Bank Ref No contains it
(case-insensitive) -- see services/refund_check.py's
filter_by_payu_search(). Filtering is display-only (applied after
write-through, not before -- see refund_check_page()'s comment), so an
active search never pauses the self-healing refund-status refresh for
rows it hides.

Hide-refunded split (2026-10-21, per explicit request: "the data
displayed looks clumsy... show only those orders for which refund is
not triggered and hide orders that already received refund... allow
user to unhide refunded transactions"): each table's rows are split via
services/refund_check.py's split_by_refund_status() into non_refunded
(rendered directly, most-recent-first) and refunded (rendered inside a
collapsible <details> section, collapsed by default -- opening it is
the "unhide" gesture). Search is applied to BOTH halves independently
("it should be searchable as well from the same search box") -- a
search matching a refunded order still surfaces it, and the template
auto-expands the refunded section when a search actually matches
something inside it, so a match is never hidden behind an extra click.

Lookback window (2026-10-21, per explicit request: "store the orders
for last 1 week (T-6 to T) from today instead of T-2 to T"):
LOOKBACK_DAYS is 7 (today + previous 6 days), up from 3.

PayU fetch bug, fixed same day: widening the window to 7 days exposed a
real bug in _fetch_payu_transactions() -- PayU's own get_Transaction_Details
response is capped (confirmed live: a single multi-day call returned
exactly 1000 rows, all from the OLDEST 4 days, with the 3 most recent
days -- including today -- silently missing, no error). That directly
caused the "Payment Received by PayU but Not by Machine" table to come
back completely empty (its matching requires a PayU transaction match,
impossible for any order on a day PayU's response dropped). Fixed by
fetching one day at a time (_date_range()) and concatenating, same
asyncio.gather concurrency pattern as _fetch_payu_refunds() -- each
day's response stays safely under the cap regardless of how wide
LOOKBACK_DAYS is.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, get_settings, require_admin
from backend.templating import templates
from db.models import OrderPaymentRecord, RefundCheck, UnrecordedPaymentCheck, User
from monitoring.config import Settings
from monitoring.models import MonitoringError
from orders.mapping import IST_TZ
from services.payu_client import PayUClient, PayUNotConfiguredError, PayURefund, PayUTransaction
from services.refund_check import (
    RefundDecision,
    append_trace,
    build_failed_transactions,
    build_unrecorded_payments,
    decide_refund_action,
    filter_by_payu_search,
    index_payu_transactions_by_txnid,
    is_failed_transaction,
    is_unrecorded_payment,
    split_by_refund_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()

LOOKBACK_DAYS = 7  # today + previous 6 days (T-6 to T)
MIN_ORDER_AGE_SECONDS = 150  # ~120s juice-dispense time + buffer -- see module docstring
TEST_DEVICE_APP = "TEST-REFUND-PIPELINE"  # marks a dummy order seeded against the real PayU TEST account -- see README

# Kill-switch for the refund-TRIGGER pipeline specifically (NOT the
# read-only PayU status display, which keeps working regardless -- see
# module docstring). Active (2026-10-20, per explicit instruction) --
# the trigger now resolves PRODUCTION PayU credentials for a real
# order and sends real cancel_refund_transaction requests with real
# money, same as the read-only display already did. Still only ever
# reachable from an admin's confirmed button click (see module
# docstring's "Keeping the constraint on refund authorization intact"
# paragraph for the full guarantee) -- this flag does not change that.
# Shared by BOTH refund-trigger routes below (failed transactions and
# unrecorded payments) -- one kill-switch, not two, so they can't drift
# out of sync. Flip back to False to pause the whole pipeline again.
REFUND_TRIGGER_ENABLED = True


def _today_ist() -> str:
    return datetime.now(timezone.utc).astimezone(IST_TZ).strftime("%Y-%m-%d")


def _lookback_start(today: str, days: int = LOOKBACK_DAYS) -> str:
    return (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def _date_range(start: str, end: str) -> list[str]:
    """Every 'YYYY-MM-DD' from start to end, inclusive. See
    _fetch_payu_transactions() for why this matters: PayU's response
    needs fetching one day at a time, not as a single multi-day range."""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    days = (d1 - d0).days
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


def _test_payu_settings(settings: Settings) -> Settings:
    """A Settings copy pointed at the TEST/sandbox PayU account -- used
    for any PayU call (lookup OR trigger) belonging to a TEST_DEVICE_APP
    dummy order specifically (see _payu_settings_for()), so that dormant
    verification path never touches production credentials. A real
    order's own trigger/lookup calls resolve production settings
    directly instead -- see module docstring's "Refund-trigger
    activation" paragraph."""
    return replace(
        settings,
        payu_merchant_key=settings.payu_test_merchant_key,
        payu_merchant_salt=settings.payu_test_merchant_salt,
        payu_env="test",
    )


def _payu_settings_for(order: OrderPaymentRecord, settings: Settings) -> Settings:
    """Which PayU account a given order's own records actually live in
    -- test for seeded dummy orders, production for everything else."""
    return _test_payu_settings(settings) if order.device_app == TEST_DEVICE_APP else settings


def _refund_token_for_order(order_id: str) -> str:
    """Merchant-generated idempotency key PayU itself also enforces --
    a reused token comes back {"status": 0, "msg": "token already used
    or request pending"} per PayU's own docs. Max 23 chars; derived
    deterministically from the order so a genuine retry (one that never
    got a definitive PayU response, so our own lock never got set)
    reuses the same token rather than minting a new one each time."""
    return f"RF{order_id}"[:23]


async def _fetch_payu_transactions(client_settings: Settings, start: str, today: str) -> tuple[list[PayUTransaction], str | None]:
    """get_transaction_details for one credential scope -- shared by
    both tables, since each is just a different lens on the same raw
    PayU transaction list for the window (matched via mihpayid for
    failed transactions, via txnid for unrecorded payments). Best-effort
    -- a PayU hiccup must not take the whole page down.

    Fetched ONE DAY AT A TIME, not as a single start/end range (fixed
    2026-10-21, after widening LOOKBACK_DAYS to 7 exposed this): PayU's
    own get_Transaction_Details response is capped -- confirmed live, a
    single call for the 7-day window returned exactly 1000 rows, every
    one of them from the OLDEST 4 days, with the 3 most recent days
    (including today) silently missing entirely, no error returned.
    That's exactly backwards for a page about today's/yesterday's
    failures -- it was the direct cause of the "Payment Received by
    PayU but Not by Machine" table coming back completely empty (that
    table's matching requires a PayU transaction match, so any order
    from a day PayU's response happened to drop can never match).
    Fetching per day (observed live volume: ~150-350 rows/day, safely
    under the cap) and concatenating avoids this regardless of how wide
    LOOKBACK_DAYS ever gets -- concurrently via asyncio.gather, same
    pattern as _fetch_payu_refunds(), so widening the window doesn't
    add sequential latency either."""
    if not client_settings.payu_configured:
        return [], None
    days = _date_range(start, today)
    client = PayUClient(client_settings)
    try:
        results = await asyncio.gather(
            *(client.get_transaction_details(day, day) for day in days),
            return_exceptions=True,
        )
    finally:
        await client.close()

    txns: list[PayUTransaction] = []
    error: str | None = None
    for day, result in zip(days, results):
        if isinstance(result, PayUNotConfiguredError):
            error = error or str(result)
        elif isinstance(result, MonitoringError):
            error = error or f"Could not fetch PayU status for {day} ({result.state.value}): {result}"
        elif isinstance(result, BaseException):
            raise result
        else:
            txns.extend(result)
    return txns, error


async def _fetch_payu_refunds(client_settings: Settings, txnids: list[str]) -> dict[str, list[PayURefund]]:
    """getAllRefundsFromTxnIds for a batch of txnids, concurrently, for
    one credential scope. Shared by both tables -- both match/key
    refunds by order_code/txnid (confirmed live -- see
    services/payu_client.py's GET_ALL_REFUNDS_COMMAND comment)."""
    if not txnids or not client_settings.payu_configured:
        return {}
    client = PayUClient(client_settings)
    refunds_by_txnid: dict[str, list[PayURefund]] = {}
    try:
        results = await asyncio.gather(
            *(client.get_refunds_for_transaction(txnid) for txnid in txnids),
            return_exceptions=True,
        )
        for txnid, result in zip(txnids, results):
            if isinstance(result, BaseException):
                logger.warning("Could not fetch PayU refunds for %s: %s", txnid, result)
                continue
            refunds_by_txnid[txnid] = result
    finally:
        await client.close()
    return refunds_by_txnid


def _write_through_refund_status(db: Session, check_cls, checks_by_order_id: dict, rows: list, orders_by_id: dict, checked_at: datetime) -> None:
    """Shared self-healing write-through cache for either table: whatever
    PayU's live lookup just confirmed becomes this row's persisted state,
    so the table catches up on its own ("update the refund status once
    refund is completed") without a separate poller. Display-only --
    does NOT touch the idempotency lock (refund_api_requested_at); only
    a POST route ever sets that, so a passive page view can never lock
    an order an admin hasn't acted on. Only traced when something is
    actually new (first sighting, or a status change) -- otherwise every
    page refresh would add an identical, noisy trace line. check_cls is
    RefundCheck or UnrecordedPaymentCheck -- both have identical shape
    for this purpose (duck-typed, see services/refund_check.py's
    decide_refund_action()/append_trace())."""
    for row in rows:
        if row.payu_refund_id is None:
            continue
        rc = checks_by_order_id.get(row.order_id)
        is_new_sighting = rc is None or rc.payu_refund_token is None
        status_changed = rc is not None and rc.payu_refund_status != row.payu_refund_status
        if rc is None:
            rc = check_cls(order_id=row.order_id)
            db.add(rc)
            checks_by_order_id[row.order_id] = rc
        if is_new_sighting or status_changed:
            order = orders_by_id.get(row.order_id)
            account = "TEST" if order and order.device_app == TEST_DEVICE_APP else "production"
            append_trace(
                rc,
                f"GET /refund-check — getAllRefundsFromTxnIds ({account} account, txnid={row.order_code}) "
                f"→ refund_id={row.payu_refund_id}, status={row.payu_refund_status}",
                now=checked_at,
            )
        rc.payu_refund_token = row.payu_refund_id
        rc.payu_refund_status = row.payu_refund_status
        rc.payu_refund_checked_at = checked_at


@router.get("/refund-check", response_class=HTMLResponse)
async def refund_check_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    search: str | None = Query(None, description="Search by PayU ID or Bank Ref No, across both tables"),
):
    today = _today_ist()
    start = _lookback_start(today)
    age_cutoff = datetime.now(timezone.utc) - timedelta(seconds=MIN_ORDER_AGE_SECONDS)

    orders = db.execute(
        select(OrderPaymentRecord).where(
            OrderPaymentRecord.order_date >= start, OrderPaymentRecord.order_date <= today,
            OrderPaymentRecord.created_at_target <= age_cutoff,
        )
    ).scalars().all()
    orders_by_id = {o.order_id: o for o in orders}

    refund_checks = db.execute(
        select(RefundCheck).where(RefundCheck.order_id.in_(orders_by_id))
    ).scalars().all()
    refund_checks_by_order_id = {rc.order_id: rc for rc in refund_checks}

    unrecorded_checks = db.execute(
        select(UnrecordedPaymentCheck).where(UnrecordedPaymentCheck.order_id.in_(orders_by_id))
    ).scalars().all()
    unrecorded_checks_by_order_id = {rc.order_id: rc for rc in unrecorded_checks}

    # Step 1: fetch raw PayU transactions for the window, once per
    # credential scope actually present (production always; test too,
    # only if a TEST_DEVICE_APP dummy order is in view). This one fetch
    # serves BOTH tables below -- failed transactions match it via
    # mihpayid, unrecorded payments via txnid -- so showing the second
    # table costs no extra get_Transaction_Details calls.
    prod_orders = [o for o in orders if o.device_app != TEST_DEVICE_APP]
    test_orders = [o for o in orders if o.device_app == TEST_DEVICE_APP]

    prod_txns, payu_error = await _fetch_payu_transactions(settings, start, today)
    payu_txns = list(prod_txns)
    if test_orders:
        test_txns, test_error = await _fetch_payu_transactions(_test_payu_settings(settings), start, today)
        payu_txns += test_txns
        payu_error = payu_error or test_error

    # Step 2: now that we know every PayU transaction in the window,
    # figure out which order_codes actually need a refund-status lookup
    # -- the union of both tables' real candidates, split by which
    # credential scope their own order belongs to, so each txnid is
    # looked up exactly once against the right account.
    payu_txns_by_txnid = index_payu_transactions_by_txnid(payu_txns)

    def _candidate_txnids(order_list: list[OrderPaymentRecord]) -> list[str]:
        codes = set()
        for o in order_list:
            if not o.order_code:
                continue
            if is_failed_transaction(o):
                codes.add(o.order_code)
            elif is_unrecorded_payment(o, payu_txns_by_txnid.get(o.order_code)):
                codes.add(o.order_code)
        return sorted(codes)

    prod_refunds = await _fetch_payu_refunds(settings, _candidate_txnids(prod_orders))
    payu_refunds_by_txnid = dict(prod_refunds)
    if test_orders:
        test_refunds = await _fetch_payu_refunds(_test_payu_settings(settings), _candidate_txnids(test_orders))
        payu_refunds_by_txnid.update(test_refunds)

    rows = build_failed_transactions(orders, refund_checks_by_order_id, payu_txns, payu_refunds_by_txnid)
    unrecorded_rows = build_unrecorded_payments(orders, unrecorded_checks_by_order_id, payu_txns, payu_refunds_by_txnid)

    checked_at = datetime.now(timezone.utc)
    # Write-through runs against the FULL (unfiltered) row lists -- a
    # search in progress must not pause self-healing status refresh for
    # rows it happens to hide this request; only the template's display
    # below is narrowed by search.
    _write_through_refund_status(db, RefundCheck, refund_checks_by_order_id, rows, orders_by_id, checked_at)
    _write_through_refund_status(db, UnrecordedPaymentCheck, unrecorded_checks_by_order_id, unrecorded_rows, orders_by_id, checked_at)

    # Hide-refunded split: each table's rows become (non_refunded,
    # refunded) -- the refunded half is rendered collapsed in the
    # template, an admin's way to "unhide" it. Search narrows BOTH
    # halves independently, so a refunded match is still findable.
    non_refunded, refunded = split_by_refund_status(rows)
    unrecorded_non_refunded, unrecorded_refunded = split_by_refund_status(unrecorded_rows)

    return templates.TemplateResponse(
        request, "refund_check.html",
        {
            "user": admin,
            "start": start,
            "end": today,
            "rows": filter_by_payu_search(non_refunded, search),
            "refunded_rows": filter_by_payu_search(refunded, search),
            "unrecorded_rows": filter_by_payu_search(unrecorded_non_refunded, search),
            "refunded_unrecorded_rows": filter_by_payu_search(unrecorded_refunded, search),
            "search": search or "",
            "payu_configured": settings.payu_configured,
            "payu_error": payu_error,
            "refund_trigger_enabled": REFUND_TRIGGER_ENABLED,
        },
    )


async def _handle_refund_trigger(
    db: Session, admin: User, order_id: str,
    check_cls, refund_mihpayid: str | None, order_code: str | None,
    lookup_settings: Settings, lookup_account: str,
) -> None:
    """Shared idempotency-check + trigger core for both refund-trigger
    routes below. Caller has already resolved which PayU id to refund
    (refund_mihpayid), which order_code to check existing refunds
    against, and which PayU account this order's own records actually
    live in (lookup_settings/lookup_account -- production for any real
    order, test/sandbox only for a dormant TEST_DEVICE_APP dummy order,
    see _payu_settings_for()) -- see each route for how refund_mihpayid/
    order_code differ between the two categories. The SAME
    lookup_settings is used for both the pre-trigger existing-refund
    check below AND the actual initiate_refund() call if it gets that
    far -- a real order's refund genuinely goes to PayU's production
    account this way (per explicit instruction, 2026-10-20). Mutates
    `existing` in place; caller commits via the normal FastAPI DB-session
    lifecycle."""
    existing = db.execute(select(check_cls).where(check_cls.order_id == order_id)).scalar_one_or_none()
    if existing is None:
        existing = check_cls(order_id=order_id)
        db.add(existing)
    if not existing.refund_requested:
        existing.refund_requested = True
        existing.requested_by_user_id = admin.id
        existing.requested_at = datetime.now(timezone.utc)
    append_trace(existing, f"POST clicked by {admin.email}")

    if not refund_mihpayid or not order_code:
        append_trace(existing, "No matching order (or missing PayU reference) in the current lookback window — nothing to act on.")
        return

    if not lookup_settings.payu_configured:
        missing = "PAYU_TEST_MERCHANT_KEY/PAYU_TEST_MERCHANT_SALT" if lookup_account == "TEST" else "PAYU_MERCHANT_KEY/PAYU_MERCHANT_SALT"
        existing.refund_api_error = f"{missing} are not set in .env — refund triggering needs real PayU credentials for this order's own ({lookup_account}) account."
        append_trace(existing, f"Aborted — {missing} not configured. No PayU call made.")
        return

    live_refund: PayURefund | None = None
    append_trace(existing, f"Step 1: checking for an existing refund — getAllRefundsFromTxnIds ({lookup_account} account, txnid={order_code})")
    lookup_client = PayUClient(lookup_settings)
    try:
        refunds = await lookup_client.get_refunds_for_transaction(order_code)
        live_refund = refunds[0] if refunds else None
        append_trace(
            existing,
            f"→ {'found existing refund: token=' + live_refund.refund_token + ', status=' + live_refund.status if live_refund else 'no existing refund found'}",
        )
    except (PayUNotConfiguredError, MonitoringError) as e:
        logger.warning("Could not check existing PayU refund status for %s before triggering: %s", order_id, e)
        append_trace(existing, f"→ lookup failed ({e}) — proceeding as if no existing refund was found.")
    finally:
        await lookup_client.close()

    decision = decide_refund_action(existing, live_refund)

    if decision == RefundDecision.ALREADY_LOCKED:
        append_trace(existing, "Step 2: already locked (a definitive PayU response was already recorded for this order) — ignored, no PayU call made.")

    elif decision == RefundDecision.BACKFILL_FROM_PAYU:
        now = datetime.now(timezone.utc)
        existing.payu_refund_token = live_refund.refund_token
        existing.payu_refund_status = live_refund.status
        existing.payu_refund_checked_at = now
        existing.refund_api_requested_at = now
        existing.refund_api_error = "Refund already existed on PayU (not triggered by this app)."
        existing.refund_mode = lookup_account.lower()
        append_trace(existing, "Step 2: PayU already shows a refund for this order — recorded it, cancel_refund_transaction was NOT called.")

    elif decision == RefundDecision.TRIGGER:
        token = _refund_token_for_order(order_id)
        order = db.execute(select(OrderPaymentRecord).where(OrderPaymentRecord.order_id == order_id)).scalar_one_or_none()
        amount = order.order_money if order else None
        append_trace(
            existing,
            f"Step 2: triggering refund — cancel_refund_transaction ({lookup_account} account, mihpayid={refund_mihpayid}, "
            f"token={token}, amount={amount})",
        )
        trigger_client = PayUClient(lookup_settings)
        try:
            result = await trigger_client.initiate_refund(mihpayid=refund_mihpayid, token=token, amount=amount or 0)
        except (PayUNotConfiguredError, MonitoringError) as e:
            existing.refund_api_error = str(e)
            append_trace(existing, f"→ request failed before reaching PayU: {e} — NOT locked, retry allowed.")
            await trigger_client.close()
            return
        await trigger_client.close()

        existing.refund_api_requested_at = datetime.now(timezone.utc)
        existing.refund_mode = lookup_account.lower()
        if result.success:
            existing.payu_refund_status = "queued"
            existing.refund_api_error = None
            append_trace(existing, f"→ PayU accepted the request: \"{result.message}\" (request_id={result.request_id}). Locked.")
        else:
            existing.refund_api_error = result.message or "PayU rejected the refund request."
            append_trace(existing, f"→ PayU rejected the request: \"{result.message}\". Locked (same request would fail again).")


@router.post("/refund-check/{order_id}/refund")
async def request_refund(
    order_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Admin-authorized refund trigger for a FAILED TRANSACTION (payment
    completed, order/delivery didn't) -- the confirm() dialog on the
    button itself (refund_check.html) is the "allow admin to
    authenticate it" gesture this was explicitly asked for; there's no
    separate re-auth step beyond the admin already being logged in.

    Idempotent and safety-gated -- see module docstring for both. Always
    records the click as local intent (refund_requested) first, exactly
    as this route did before this feature existed, then runs the actual
    decision/trigger logic on top of that.
    """
    if not REFUND_TRIGGER_ENABLED:
        existing = db.execute(select(RefundCheck).where(RefundCheck.order_id == order_id)).scalar_one_or_none()
        if existing is None:
            existing = RefundCheck(order_id=order_id)
            db.add(existing)
        append_trace(existing, f"POST /refund-check/{order_id}/refund received from {admin.email} — refund triggering is currently disabled. No PayU call made.")
        return RedirectResponse(url="/refund-check", status_code=303)

    order = db.execute(select(OrderPaymentRecord).where(OrderPaymentRecord.order_id == order_id)).scalar_one_or_none()
    lookup_settings = _payu_settings_for(order, settings) if order else settings
    lookup_account = "TEST" if order and order.device_app == TEST_DEVICE_APP else "production"
    await _handle_refund_trigger(
        db, admin, order_id, RefundCheck,
        refund_mihpayid=order.out_trade_no if order else None,
        order_code=order.order_code if order else None,
        lookup_settings=lookup_settings, lookup_account=lookup_account,
    )
    return RedirectResponse(url="/refund-check", status_code=303)


@router.post("/refund-check/unrecorded-payment/{order_id}/refund")
async def request_unrecorded_payment_refund(
    order_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Admin-authorized refund trigger for an UNRECORDED PAYMENT (PayU
    shows it paid, the machine doesn't) -- same gesture, idempotency,
    and safety posture as request_refund() above ("functionality for
    these orders should be similar to" the failed-transaction pipeline,
    per explicit request). The one real difference: there's no reliable
    out_trade_no to refund against here (the machine's own record of
    this order is exactly what's in question) -- the PayU id to refund
    is read off the matched PayU transaction itself (order_code ==
    txnid), fetched fresh here rather than reused from the GET page's
    cache, same "POST does its own I/O" convention as request_refund().
    """
    if not REFUND_TRIGGER_ENABLED:
        existing = db.execute(select(UnrecordedPaymentCheck).where(UnrecordedPaymentCheck.order_id == order_id)).scalar_one_or_none()
        if existing is None:
            existing = UnrecordedPaymentCheck(order_id=order_id)
            db.add(existing)
        append_trace(existing, f"POST /refund-check/unrecorded-payment/{order_id}/refund received from {admin.email} — refund triggering is currently disabled. No PayU call made.")
        return RedirectResponse(url="/refund-check", status_code=303)

    order = db.execute(select(OrderPaymentRecord).where(OrderPaymentRecord.order_id == order_id)).scalar_one_or_none()
    refund_mihpayid: str | None = None
    lookup_settings = _payu_settings_for(order, settings) if order else settings
    lookup_account = "TEST" if order and order.device_app == TEST_DEVICE_APP else "production"

    if order is not None and order.order_code is not None and lookup_settings.payu_configured:
        # Resolve the PayU transaction this order actually matches, to
        # get its mihpayid -- out_trade_no isn't trustworthy here, since
        # the machine's own record of this order is exactly what's in
        # question. Narrow fetch: just this order's own IST date.
        txns, _ = await _fetch_payu_transactions(lookup_settings, order.order_date, order.order_date)
        matched = index_payu_transactions_by_txnid(txns).get(order.order_code)
        if matched is not None and matched.mihpayid:
            refund_mihpayid = matched.mihpayid

    await _handle_refund_trigger(
        db, admin, order_id, UnrecordedPaymentCheck,
        refund_mihpayid=refund_mihpayid,
        order_code=order.order_code if order else None,
        lookup_settings=lookup_settings, lookup_account=lookup_account,
    )
    return RedirectResponse(url="/refund-check", status_code=303)
