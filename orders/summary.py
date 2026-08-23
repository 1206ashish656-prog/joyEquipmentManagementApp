"""
Computes the daily order summary — aggregated ("ALL") and machine-wise
("device_app") rows — from a day's fetched OrderRecords.

Filter (confirmed with the user 2026-08-24): only orders where
order_status == "Completed" AND delivery_status == "Success" count toward
the numbers below. This mirrors the rest of the codebase's rule: a
summary is computed from explicit, filtered data — never silently
padded/guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .mapping import OrderRecord
from .selectors import SUMMARY_FILTER

ALL_DEVICES_LABEL = "ALL"


@dataclass
class SummaryRow:
    date: str
    device_app: str
    number_of_orders: int
    average_price: Decimal
    total_number_of_oranges: int
    average_juice_weight: Decimal


def passes_summary_filter(order: OrderRecord) -> bool:
    return (
        order.order_status == SUMMARY_FILTER["order_status"]
        and order.delivery_status == SUMMARY_FILTER["delivery_status"]
    )


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _summarize_group(date: str, device_app: str, orders: list[OrderRecord]) -> SummaryRow:
    n = len(orders)
    total_price = sum((o.order_money for o in orders), Decimal("0"))
    total_oranges = sum(o.orange_num for o in orders)
    total_juice_weight = sum((o.orange_weight for o in orders), Decimal("0"))

    return SummaryRow(
        date=date,
        device_app=device_app,
        number_of_orders=n,
        average_price=_round2(total_price / n) if n else Decimal("0.00"),
        total_number_of_oranges=total_oranges,
        average_juice_weight=_round2(total_juice_weight / n) if n else Decimal("0.00"),
    )


def compute_daily_summary(date: str, orders: list[OrderRecord]) -> list[SummaryRow]:
    """Returns one aggregate row (device_app='ALL') plus one row per
    device_app, all computed only from orders passing the summary filter.
    An empty (but non-error) result for a day with genuinely zero
    qualifying orders still produces an 'ALL' row with number_of_orders=0
    — this is a legitimate business outcome (a quiet day), not a
    validation failure; that distinction matters (see project principle:
    absence of *data* is an error, absence of *qualifying orders* on a day
    that clearly returned real rows is not)."""
    filtered = [o for o in orders if passes_summary_filter(o)]

    rows = [_summarize_group(date, ALL_DEVICES_LABEL, filtered)]

    by_device: dict[str, list[OrderRecord]] = {}
    for o in filtered:
        by_device.setdefault(o.device_app, []).append(o)

    for device_app in sorted(by_device):
        rows.append(_summarize_group(date, device_app, by_device[device_app]))

    return rows
