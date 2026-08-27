"""
Keeps TODAY's (IST — India Standard Time) order summary continuously up
to date.

Why this exists: orders/backfill.py deliberately only ever processes a
date once it's fully elapsed — a day still accumulating orders would
otherwise get permanently marked SUCCESS on a partial snapshot the first
time anyone looked at it, and backfill.py's whole caching model treats
SUCCESS as "done, never re-fetch." That's exactly right for history, and
exactly wrong for "today": nobody sees today's orders in the Order
Summary tab until tomorrow, when today finally counts as a complete past
day and a manual/scheduled backfill run picks it up.

This module is the deliberate exception to that rule: it always targets
"today" (recomputed fresh every cycle, in IST — see orders/mapping.py's
IST_TZ, NOT the target's own UTC+8 or server-local time) and
unconditionally overwrites, whether or not that date already has a
SUCCESS row from an earlier, now-stale, cycle. orders/store.py's
save_day() is already idempotent (delete-then-reinsert), so repeated
calls for the same date are safe — this is the only piece that's new.

Also reconciles automatically at startup (reconcile_after_downtime()):
if the app was down when the IST date rolled over, the day it was last
updating gets a forced final refresh, and any fully-elapsed days in
between that were never touched at all get backfilled — see that
function's docstring for why run_cycle's own rollover logic alone isn't
enough for this.

Run:
    python -m orders.realtime_worker --loop                  # every 5 min, forever
    python -m orders.realtime_worker --loop --interval 120    # every 2 min
    python -m orders.realtime_worker --once                   # refresh today once and exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db import base as db_base
from db.models import OrderSummaryRun
from monitoring.config import load_settings
from monitoring.models import MonitoringError

from .backfill import run_backfill
from .client import OrdersClient
from .mapping import IST_TZ
from .store import save_day
from .summary import compute_daily_groups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("orders.realtime_worker")

DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes — frequent enough to feel "live" on a dashboard refresh, not so frequent it hammers the target for a slowly-accumulating count.


def today_ist(now: datetime | None = None) -> str:
    """`now` is injectable (defaults to the real current instant) purely
    so tests can pin a specific moment rather than depending on when
    they happen to run."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(IST_TZ).strftime("%Y-%m-%d")


def _day_after(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _day_before(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


async def refresh_date(client: OrdersClient, date: str) -> bool:
    """Fetch + recompute + save ONE IST date, unconditionally — no
    already-cached check, since the entire point is overwriting a
    snapshot that may already exist. Returns True on success, False on a
    fetch failure (logged, not raised — a transient failure this cycle
    must not kill the loop; the next cycle just tries again)."""
    try:
        records = await client.fetch_day(date)
    except MonitoringError as e:
        logger.error("%s: fetch failed (%s) — will retry next cycle", date, e)
        return False

    groups = compute_daily_groups(date, records)
    with db_base.get_session() as db_session:
        save_day(db_session, date, groups, raw_orders_fetched=len(records))

    qualifying = sum(g.number_of_orders for g in groups)
    logger.info(
        "%s: refreshed — %d raw orders fetched (IST day), %d qualifying, %d (machine,price,pay_type) groups",
        date, len(records), qualifying, len(groups),
    )
    return True


async def reconcile_after_downtime(client: OrdersClient, today_fn=today_ist, backfill_fn=run_backfill) -> None:
    """Runs once at startup, before any continuous cycles begin, to close
    whatever gap downtime left in Order Summary.

    run_cycle()'s own day-rollover handling only closes a gap that
    happens WHILE the process is running (it carries last_seen_date
    between its own iterations in memory) — a freshly started process
    has no such memory. If the app was down when the IST calendar date
    rolled over, the day it was last actively updating never gets its
    final "day-end" refresh: OrderSummaryRun already has a SUCCESS row
    for that date from its last live cycle, so it just stays frozen on
    that partial snapshot — orders/backfill.py's normal cached-date skip
    would even leave it stale if someone ran a manual backfill
    afterwards, since "already SUCCESS" reads as "already done."
    """
    today = today_fn()

    with db_base.get_session() as db_session:
        last_known = db_session.execute(select(func.max(OrderSummaryRun.date))).scalar()

    if last_known is None or last_known == today:
        return  # first-ever run, or restarted the same IST day — nothing to reconcile

    logger.info("Startup reconciliation: last known order data is %s, today is %s", last_known, today)

    # The last active day may have been cut off mid-day — force a fresh
    # refresh regardless of its existing SUCCESS status (mirrors
    # run_cycle's own "day that just ended" refresh).
    await refresh_date(client, last_known)

    # Any fully-elapsed days strictly between last_known and today that
    # were never touched at all (a longer outage) — normal backfill
    # semantics apply (skip already-cached, fetch missing).
    yesterday = _day_before(today)
    gap_start = _day_after(last_known)
    if gap_start <= yesterday:
        logger.info("Backfilling missing days %s through %s", gap_start, yesterday)
        await backfill_fn(gap_start, yesterday)


async def run_once() -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()
    client = OrdersClient(settings)
    try:
        await reconcile_after_downtime(client)
        await refresh_date(client, today_ist())
    finally:
        await client.close()


async def run_cycle(client: OrdersClient, last_seen_date: str | None, today_fn=today_ist) -> str:
    """One iteration's worth of work: refresh today, plus — if the IST
    calendar date rolled over since the previous cycle — one final
    refresh of the day that just ended. Returns the date to pass back in
    as `last_seen_date` next time. Factored out of run_loop so the
    rollover logic is testable without an actual sleep loop."""
    # Cheap (a small file read) and safe to do unconditionally every
    # cycle, whether or not a re-auth actually happened elsewhere — see
    # OrdersClient.reload_cookies()'s own docstring for why this matters
    # when running alongside monitoring.worker in the same process
    # (monitoring/combined_worker.py).
    client.reload_cookies()

    current = today_fn()

    if last_seen_date is not None and current != last_seen_date:
        # The day that just ended never gets touched again otherwise:
        # backfill.py sees it already has a SUCCESS row (from an earlier
        # cycle here) and skips it, so whatever orders landed between
        # the last tick and midnight would be silently missing forever.
        logger.info("IST date rolled over (%s -> %s) — final refresh of %s", last_seen_date, current, last_seen_date)
        await refresh_date(client, last_seen_date)

    await refresh_date(client, current)
    return current


async def run_loop(interval_seconds: int) -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()
    client = OrdersClient(settings)

    last_seen_date: str | None = None
    try:
        await reconcile_after_downtime(client)
        while True:
            last_seen_date = await run_cycle(client, last_seen_date)
            await asyncio.sleep(interval_seconds)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep today's (IST) order summary continuously up to date")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true", help="Run forever, refreshing today every --interval seconds")
    mode.add_argument("--once", action="store_true", help="Refresh today once and exit")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between refreshes in --loop mode (default {DEFAULT_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_once())
    else:
        asyncio.run(run_loop(args.interval))


if __name__ == "__main__":
    main()
