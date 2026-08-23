"""
Unit tests for orders/rollup.py — grouping stored (date, device_app,
price, pay_type) rows into whatever view is requested, with correct
orders-weighted averages when multiple groups get combined.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from orders.rollup import rollup


@dataclass
class FakeRow:
    date: str
    device_app: str
    price: Decimal
    pay_type: str
    number_of_orders: int
    total_number_of_oranges: int
    average_juice_weight: Decimal


def _rows():
    return [
        FakeRow("2026-08-23", "NEXUS", Decimal("120.00"), "UPI", 3, 9, Decimal("200.00")),
        FakeRow("2026-08-23", "NEXUS", Decimal("150.00"), "UPI", 2, 6, Decimal("220.00")),
        FakeRow("2026-08-23", "Gravity", Decimal("100.00"), "Cash", 5, 10, Decimal("180.00")),
        FakeRow("2026-08-24", "NEXUS", Decimal("120.00"), "UPI", 4, 12, Decimal("210.00")),
    ]


def test_full_aggregate_no_grouping():
    result = rollup(_rows(), group_by=())
    assert len(result) == 1
    r = result[0]
    assert r.key == {}
    assert r.number_of_orders == 3 + 2 + 5 + 4  # 14
    assert r.total_number_of_oranges == 9 + 6 + 10 + 12  # 37


def test_weighted_average_price_across_combined_groups():
    # NEXUS on 2026-08-23: 3 orders @120 + 2 orders @150
    # weighted avg = (3*120 + 2*150) / 5 = (360+300)/5 = 132.00
    rows = [r for r in _rows() if r.date == "2026-08-23" and r.device_app == "NEXUS"]
    result = rollup(rows, group_by=("device_app",))
    assert len(result) == 1
    assert result[0].number_of_orders == 5
    assert result[0].average_price == Decimal("132.00")


def test_group_by_device_app_keeps_machines_separate():
    result = rollup(_rows(), group_by=("device_app",))
    by_device = {r.key["device_app"]: r for r in result}
    assert set(by_device) == {"NEXUS", "Gravity"}
    assert by_device["Gravity"].number_of_orders == 5
    assert by_device["Gravity"].average_price == Decimal("100.00")


def test_group_by_device_and_price_merges_across_dates():
    # _rows() has NEXUS/120.00 on both 2026-08-23 (3 orders) and
    # 2026-08-24 (4 orders) -- grouping by (device_app, price) without
    # `date` correctly merges those into one 7-order group, since date
    # wasn't included as a grouping dimension.
    result = rollup(_rows(), group_by=("device_app", "price"))
    assert len(result) == 3
    keys = {(r.key["device_app"], r.key["price"]) for r in result}
    assert keys == {
        ("NEXUS", Decimal("120.00")),
        ("NEXUS", Decimal("150.00")),
        ("Gravity", Decimal("100.00")),
    }
    nexus_120 = next(r for r in result if r.key == {"device_app": "NEXUS", "price": Decimal("120.00")})
    assert nexus_120.number_of_orders == 3 + 4  # merged across both dates


def test_group_by_date_device_and_price_keeps_all_four_distinct():
    result = rollup(_rows(), group_by=("date", "device_app", "price"))
    assert len(result) == 4


def test_time_series_by_date_sorted_chronologically():
    result = rollup(_rows(), group_by=("date",))
    dates = [r.key["date"] for r in result]
    assert dates == ["2026-08-23", "2026-08-24"]


def test_time_series_by_date_and_device():
    result = rollup(_rows(), group_by=("date", "device_app"))
    keys = {(r.key["date"], r.key["device_app"]) for r in result}
    assert ("2026-08-23", "NEXUS") in keys
    assert ("2026-08-24", "NEXUS") in keys
    assert ("2026-08-23", "Gravity") in keys


def test_group_by_pay_type():
    result = rollup(_rows(), group_by=("pay_type",))
    by_type = {r.key["pay_type"]: r for r in result}
    assert set(by_type) == {"UPI", "Cash"}
    assert by_type["UPI"].number_of_orders == 3 + 2 + 4  # 9


def test_unknown_dimension_raises():
    with pytest.raises(ValueError):
        rollup(_rows(), group_by=("not_a_real_dimension",))


def test_empty_rows_returns_empty_list():
    assert rollup([], group_by=("device_app",)) == []


# --- Admin-only derived metrics: revenue and oranges/glass ---
# (each order == one glass of juice, per the existing number_of_orders
# field; see backend/templates/order_summary.html for the admin-only
# visibility gating -- these are always computed here, just not always
# rendered.)

def test_revenue_is_orders_times_average_price():
    # Gravity: 5 orders @ 100.00 -> revenue 500.00
    result = rollup(_rows(), group_by=("device_app",))
    gravity = next(r for r in result if r.key["device_app"] == "Gravity")
    assert gravity.revenue == Decimal("500.00")


def test_revenue_uses_weighted_average_price_when_combined():
    # NEXUS on 2026-08-23: 3 @ 120.00 + 2 @ 150.00 -> avg price 132.00,
    # 5 orders -> revenue 660.00 (== 3*120 + 2*150, not average*5 by
    # coincidence -- confirming the two ways of computing it agree).
    rows = [r for r in _rows() if r.date == "2026-08-23" and r.device_app == "NEXUS"]
    result = rollup(rows, group_by=("device_app",))
    r = result[0]
    assert r.revenue == Decimal("660.00")
    assert r.revenue == r.number_of_orders * r.average_price


def test_oranges_per_glass():
    # Gravity: 10 oranges / 5 orders = 2.00 oranges per glass
    result = rollup(_rows(), group_by=("device_app",))
    gravity = next(r for r in result if r.key["device_app"] == "Gravity")
    assert gravity.oranges_per_glass == Decimal("2.00")


def test_oranges_per_glass_rounds_to_two_places():
    # Full aggregate across all rows: (9+6+10+12) oranges / (3+2+5+4)
    # orders = 37/14 = 2.642857... -> rounds to 2.64 (confirms rounding,
    # not just exact division, and that it works with group_by=()).
    result = rollup(_rows(), group_by=())
    assert result[0].oranges_per_glass == Decimal("2.64")


def test_oranges_per_glass_zero_orders_does_not_divide_by_zero():
    assert rollup([], group_by=("device_app",)) == []
    # A bucket can't have zero orders in practice (every row contributes
    # >=1), but the property itself must still be safe to call on a
    # zero-order row without raising.
    from orders.rollup import RollupRow
    empty = RollupRow(key={}, number_of_orders=0, average_price=Decimal("0.00"),
                       total_number_of_oranges=0, average_juice_weight=Decimal("0.00"))
    assert empty.oranges_per_glass == Decimal("0.00")
    assert empty.revenue == Decimal("0.00")
