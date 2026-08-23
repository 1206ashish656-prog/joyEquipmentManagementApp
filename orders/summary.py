"""
Groups a day's filtered orders into (device_app, price, pay_type) groups
— the finest grain this feature stores (see db/models.py's OrderSummary
docstring for why: every rollup view is computed from these at query
time, in orders/rollup.py, rather than pre-computing every possible view).

Filter (confirmed with the user): only orders where
order_status == "Completed" AND delivery_status == "Success" count.

If price changes mid-day on the same machine, that produces two separate
groups for that machine (one per price) — this is the explicit behavior
requested, not a bug: "if there are 3 machines active and price was
altered mid-day then I expect 2 separate entries per machine, one for
each price."
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .mapping import OrderRecord
from .selectors import SUMMARY_FILTER


@dataclass
class GroupSummary:
    date: str
    device_app: str
    price: Decimal
    pay_type: str
    number_of_orders: int
    total_number_of_oranges: int
    average_juice_weight: Decimal


def passes_summary_filter(order: OrderRecord) -> bool:
    return (
        order.order_status == SUMMARY_FILTER["order_status"]
        and order.delivery_status == SUMMARY_FILTER["delivery_status"]
    )


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_daily_groups(date: str, orders: list[OrderRecord]) -> list[GroupSummary]:
    """Returns one GroupSummary per distinct (device_app, price, pay_type)
    combination present among the day's qualifying orders. A day with zero
    qualifying orders returns an empty list — that's a legitimate "quiet
    day" outcome (see OrderSummaryRun, which records that this date WAS
    processed even when this list is empty), not a validation failure."""
    filtered = [o for o in orders if passes_summary_filter(o)]

    groups: dict[tuple[str, Decimal, str], list[OrderRecord]] = {}
    for o in filtered:
        key = (o.device_app, o.order_money, o.pay_type)
        groups.setdefault(key, []).append(o)

    result = []
    for (device_app, price, pay_type), group_orders in sorted(groups.items(), key=lambda kv: kv[0]):
        n = len(group_orders)
        total_oranges = sum(o.orange_num for o in group_orders)
        total_juice_weight = sum((o.orange_weight for o in group_orders), Decimal("0"))
        result.append(
            GroupSummary(
                date=date,
                device_app=device_app,
                price=price,
                pay_type=pay_type,
                number_of_orders=n,
                total_number_of_oranges=total_oranges,
                average_juice_weight=_round2(total_juice_weight / n) if n else Decimal("0.00"),
            )
        )
    return result
