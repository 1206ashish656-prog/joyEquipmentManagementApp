"""
Rolls up stored (date, device_app, price, pay_type) groups into whatever
view the caller needs — full aggregate, machine-wise, price-wise,
pay-type-wise, any combination, or a per-date time series for charts —
by grouping on a chosen subset of dimensions and computing orders-
weighted averages for price/juice-weight across whatever gets combined.

Operates on any object exposing the same attributes as db.models.OrderSummary
(date, device_app, price, pay_type, number_of_orders,
total_number_of_oranges, average_juice_weight) — real ORM rows in
production, a plain namedtuple/dataclass in tests, no DB required to test
the math.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

ALL_DIMENSIONS = ("date", "device_app", "price", "pay_type")


@dataclass
class RollupRow:
    key: dict[str, Any]  # only the dimensions that were grouped on
    number_of_orders: int
    average_price: Decimal
    total_number_of_oranges: int
    average_juice_weight: Decimal


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rollup(rows: Iterable[Any], group_by: tuple[str, ...]) -> list[RollupRow]:
    """group_by: any subset of ALL_DIMENSIONS, in any order. An empty tuple
    produces a single row — the full aggregate across everything passed in."""
    unknown = set(group_by) - set(ALL_DIMENSIONS)
    if unknown:
        raise ValueError(f"Unknown rollup dimension(s): {unknown}")

    buckets: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = tuple(getattr(row, dim) for dim in group_by)
        bucket = buckets.setdefault(
            key, {"number_of_orders": 0, "price_weighted_sum": Decimal("0"),
                  "total_number_of_oranges": 0, "juice_weighted_sum": Decimal("0")}
        )
        n = row.number_of_orders
        bucket["number_of_orders"] += n
        bucket["price_weighted_sum"] += row.price * n
        bucket["total_number_of_oranges"] += row.total_number_of_oranges
        bucket["juice_weighted_sum"] += row.average_juice_weight * n

    results = []
    for key, b in buckets.items():
        n = b["number_of_orders"]
        results.append(
            RollupRow(
                key=dict(zip(group_by, key)),
                number_of_orders=n,
                average_price=_round2(b["price_weighted_sum"] / n) if n else Decimal("0.00"),
                total_number_of_oranges=b["total_number_of_oranges"],
                average_juice_weight=_round2(b["juice_weighted_sum"] / n) if n else Decimal("0.00"),
            )
        )

    # Stable, human-friendly ordering: by whatever dimensions were
    # selected, in ALL_DIMENSIONS order (date first, so time series come
    # out chronological).
    sort_order = [d for d in ALL_DIMENSIONS if d in group_by]
    results.sort(key=lambda r: tuple(str(r.key[d]) for d in sort_order))
    return results
