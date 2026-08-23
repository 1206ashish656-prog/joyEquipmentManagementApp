"""
Orders daily-summary backfill: for each date in a range, reuse the cached
CSV row if present, otherwise fetch that day's orders from the target,
compute the summary, and append it — sequentially, day by day (never in
parallel; the target site gets one date's worth of requests at a time).

This is the SAME code path whether run for one day (Phase 1 — the current
"extract data for 1 day and verify the numbers" step) or a full historical
range later: the range just happens to be a single day for now.

Run:
    python -m orders.backfill --date 2026-08-23
    python -m orders.backfill --start 2026-08-01 --end 2026-08-23
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from monitoring.config import PROJECT_ROOT, load_settings
from monitoring.models import MonitoringError

from .cache import append_summary_rows, load_cached_dates
from .client import OrdersClient
from .summary import ALL_DEVICES_LABEL, compute_daily_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("orders.backfill")

DEFAULT_CSV_PATH = PROJECT_ROOT / "data" / "order_summaries" / "daily_summary.csv"


def _date_range(start: str, end: str) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    if d1 < d0:
        raise ValueError(f"end date {end} is before start date {start}")
    days = (d1 - d0).days
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]


async def run_backfill(start: str, end: str, csv_path: Path = DEFAULT_CSV_PATH, force: bool = False) -> None:
    settings = load_settings()
    cached_dates = set() if force else load_cached_dates(csv_path)
    dates = _date_range(start, end)

    client = OrdersClient(settings)
    try:
        for date in dates:
            if date in cached_dates:
                logger.info("%s: already cached — skipping fetch", date)
                continue

            logger.info("%s: fetching orders from target...", date)
            try:
                records = await client.fetch_day(date)
            except MonitoringError as e:
                # A fetch failure for one day must not corrupt/skip other
                # days, and must never be recorded as "zero orders" — that
                # would silently fabricate a business number. Log and move
                # on; the date stays uncached and will be retried next run.
                logger.error("%s: fetch failed (%s) — leaving uncached, continuing to next date", date, e)
                continue

            rows = compute_daily_summary(date, records)
            append_summary_rows(csv_path, rows)

            all_row = next(r for r in rows if r.device_app == ALL_DEVICES_LABEL)
            logger.info(
                "%s: %d raw orders fetched, %d qualifying (Completed+Success) -> "
                "avg_price=%s total_oranges=%s avg_juice_weight=%s",
                date, len(records), all_row.number_of_orders,
                all_row.average_price, all_row.total_number_of_oranges, all_row.average_juice_weight,
            )
            print(f"\n=== {date} summary ===")
            for row in rows:
                print(
                    f"  {row.device_app:<10} orders={row.number_of_orders:<5} "
                    f"avg_price={row.average_price:<10} total_oranges={row.total_number_of_oranges:<6} "
                    f"avg_juice_weight={row.average_juice_weight}"
                )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill daily order summaries")
    parser.add_argument("--date", help="Single date YYYY-MM-DD (shorthand for --start/--end the same day)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if a date is already cached")
    parser.add_argument("--csv", default=str(DEFAULT_CSV_PATH), help="Path to the summary CSV")
    args = parser.parse_args()

    if args.date:
        start = end = args.date
    elif args.start and args.end:
        start, end = args.start, args.end
    else:
        parser.error("Provide either --date, or both --start and --end")
        return

    asyncio.run(run_backfill(start, end, csv_path=Path(args.csv), force=args.force))


if __name__ == "__main__":
    main()
