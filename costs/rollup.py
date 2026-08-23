"""
Rolls up CostEntry rows into whatever breakdown the caller needs — full
total, by category, by vendor, by item name, or any combination — by
summing `amount` and counting entries within each group. Simpler than
orders/rollup.py's rollup() (no weighted averages needed: cost has no
"price" that varies per unit, just a total).

Operates on any object exposing (category, vendor_name, item_name,
amount) — real ORM rows in production, a plain dataclass in tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

ALL_DIMENSIONS = ("category", "vendor_name", "item_name")


@dataclass
class CostRollupRow:
    key: dict[str, Any]  # only the dimensions that were grouped on
    total_amount: Decimal
    entry_count: int


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rollup(rows: Iterable[Any], group_by: tuple[str, ...]) -> list[CostRollupRow]:
    """group_by: any subset of ALL_DIMENSIONS, in any order. An empty
    tuple produces a single row — the full total across everything passed
    in. Rows with vendor_name=None (Staff Salaries) group under key
    value None when "vendor_name" is selected -- rendered as "—" by the
    template, not silently dropped."""
    unknown = set(group_by) - set(ALL_DIMENSIONS)
    if unknown:
        raise ValueError(f"Unknown rollup dimension(s): {unknown}")

    buckets: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(getattr(row, dim) for dim in group_by)
        bucket = buckets.setdefault(key, {"total_amount": Decimal("0"), "entry_count": 0})
        bucket["total_amount"] += row.amount
        bucket["entry_count"] += 1

    results = [
        CostRollupRow(key=dict(zip(group_by, key)), total_amount=_round2(b["total_amount"]), entry_count=b["entry_count"])
        for key, b in buckets.items()
    ]

    # Stable ordering: highest total first (the more useful default for a
    # cost breakdown — biggest spend categories/vendors up top).
    results.sort(key=lambda r: r.total_amount, reverse=True)
    return results
