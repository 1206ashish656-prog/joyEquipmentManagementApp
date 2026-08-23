"""
CSV-backed cache for daily order summaries (Phase 1 storage — the user
has flagged this will move to a real database later; this module is
intentionally the only place that knows the storage is a CSV, so that
swap stays contained).

A date is considered "already cached" when an ALL_DEVICES_LABEL row
exists for it — that row is always written last for a date-computation
(see backfill.py), so its presence is a reliable completeness marker.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from .summary import ALL_DEVICES_LABEL, SummaryRow

CSV_COLUMNS = [
    "date",
    "device_app",
    "number_of_orders",
    "average_price",
    "total_number_of_oranges",
    "average_juice_weight",
]


def load_cached_dates(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    cached: set[str] = set()
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("device_app") == ALL_DEVICES_LABEL:
                cached.add(row["date"])
    return cached


def append_summary_rows(csv_path: Path, rows: list[SummaryRow]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "date": row.date,
                    "device_app": row.device_app,
                    "number_of_orders": row.number_of_orders,
                    "average_price": str(row.average_price),
                    "total_number_of_oranges": row.total_number_of_oranges,
                    "average_juice_weight": str(row.average_juice_weight),
                }
            )


def read_all_rows(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
