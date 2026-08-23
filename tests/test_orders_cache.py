"""Unit tests for orders/cache.py — CSV round-trip and the
"already cached" detection that backfill.py relies on to skip dates."""
from __future__ import annotations

from decimal import Decimal

from orders.cache import append_summary_rows, load_cached_dates, read_all_rows
from orders.summary import SummaryRow


def _row(date, device_app, n=5):
    return SummaryRow(
        date=date, device_app=device_app, number_of_orders=n,
        average_price=Decimal("120.00"), total_number_of_oranges=n * 3,
        average_juice_weight=Decimal("205.00"),
    )


def test_missing_csv_has_no_cached_dates(tmp_path):
    assert load_cached_dates(tmp_path / "nope.csv") == set()


def test_append_then_reload_round_trips(tmp_path):
    csv_path = tmp_path / "summary.csv"
    append_summary_rows(csv_path, [_row("2026-08-23", "ALL"), _row("2026-08-23", "NEXUS")])

    rows = read_all_rows(csv_path)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-08-23"
    assert rows[0]["average_price"] == "120.00"


def test_date_only_cached_once_all_row_present(tmp_path):
    csv_path = tmp_path / "summary.csv"
    append_summary_rows(csv_path, [_row("2026-08-23", "NEXUS")])  # no ALL row yet
    assert load_cached_dates(csv_path) == set()

    append_summary_rows(csv_path, [_row("2026-08-23", "ALL")])
    assert load_cached_dates(csv_path) == {"2026-08-23"}


def test_multiple_dates_accumulate(tmp_path):
    csv_path = tmp_path / "summary.csv"
    append_summary_rows(csv_path, [_row("2026-08-22", "ALL")])
    append_summary_rows(csv_path, [_row("2026-08-23", "ALL")])
    assert load_cached_dates(csv_path) == {"2026-08-22", "2026-08-23"}


def test_append_does_not_duplicate_header(tmp_path):
    csv_path = tmp_path / "summary.csv"
    append_summary_rows(csv_path, [_row("2026-08-22", "ALL")])
    append_summary_rows(csv_path, [_row("2026-08-23", "ALL")])
    text = csv_path.read_text(encoding="utf-8")
    assert text.count("number_of_orders") == 1
