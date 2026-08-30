"""
Orders daily-summary backfill: for each date in a range, reuse the stored
DB row if that date was already processed successfully, otherwise fetch
that day's orders from the target, compute the (machine, price, pay_type)
groups, and store them — sequentially, day by day (never in parallel; the
target site gets one date's worth of requests at a time).

This is the SAME code path whether run for one day or a full historical
range — the range just happens to be a single day when verifying.

Run:
    python -m orders.backfill --date 2026-08-23
    python -m orders.backfill --start 2026-08-01 --end 2026-08-23
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from db import base as db_base
from monitoring.config import load_settings
from monitoring.models import MonitoringError

from .client import OrdersClient
from .store import get_cached_dates, mark_day_failed, save_day, save_order_payment_records
from .summary import compute_daily_groups

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("orders.backfill")


def _date_range(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    if d1 < d0:
        raise ValueError(f"end date {end} is before start date {start}")
    days = (d1 - d0).days
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


async def run_backfill(start: str, end: str, force: bool = False) -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    dates = _date_range(start, end)
    client = OrdersClient(settings)
    try:
        for date in dates:
            with db_base.get_session() as db_session:
                cached_dates = set() if force else get_cached_dates(db_session)
                if date in cached_dates:
                    logger.info("%s: already processed — skipping fetch", date)
                    continue

            logger.info("%s: fetching orders from target...", date)
            try:
                records = await client.fetch_day(date)
            except MonitoringError as e:
                # A fetch failure for one day must not corrupt/block other
                # days, and must never be recorded as a fabricated "zero
                # orders" success — it's recorded as FAILED so it's
                # visibly distinct from a genuine quiet day, and retried
                # on the next run.
                logger.error("%s: fetch failed (%s) — recording FAILED, continuing to next date", date, e)
                with db_base.get_session() as db_session:
                    mark_day_failed(db_session, date, str(e))
                continue

            groups = compute_daily_groups(date, records)

            with db_base.get_session() as db_session:
                save_day(db_session, date, groups, raw_orders_fetched=len(records))
                save_order_payment_records(db_session, date, records)

            qualifying = sum(g.number_of_orders for g in groups)
            logger.info(
                "%s: %d raw orders fetched, %d qualifying (Completed+Success), %d (machine,price,pay_type) groups",
                date, len(records), qualifying, len(groups),
            )
            print(f"\n=== {date} — {len(groups)} group(s) ===")
            for g in groups:
                print(
                    f"  {g.device_app:<10} price={g.price:<10} pay_type={g.pay_type:<6} "
                    f"orders={g.number_of_orders:<5} total_oranges={g.total_number_of_oranges:<6} "
                    f"avg_juice_weight={g.average_juice_weight}"
                )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill daily order summaries")
    parser.add_argument("--date", help="Single date YYYY-MM-DD (shorthand for --start/--end the same day)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if a date is already processed")
    args = parser.parse_args()

    if args.date:
        start = end = args.date
    elif args.start and args.end:
        start, end = args.start, args.end
    else:
        parser.error("Provide either --date, or both --start and --end")
        return

    asyncio.run(run_backfill(start, end, force=args.force))


if __name__ == "__main__":
    main()
