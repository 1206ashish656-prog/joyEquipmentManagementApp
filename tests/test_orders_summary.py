"""
Unit tests for orders/summary.py — the filter (order_status=Completed AND
delivery_status=Success) and the aggregate/machine-wise math, against
known hand-computed expected values.
"""
from __future__ import annotations

from decimal import Decimal

from orders.mapping import OrderRecord
from orders.summary import ALL_DEVICES_LABEL, compute_daily_summary, passes_summary_filter


def _order(device_app="NEXUS", order_status="Completed", delivery_status="Success",
           order_money="120.00", orange_num=3, orange_weight="200", **kw):
    return OrderRecord(
        order_id=kw.get("order_id", "1"),
        order_code=kw.get("order_code", "X"),
        device_id=kw.get("device_id", "205"),
        device_app=device_app,
        order_status=order_status,
        payment_status=kw.get("payment_status", "Have paid"),
        delivery_status=delivery_status,
        order_money=Decimal(order_money),
        orange_num=orange_num,
        orange_weight=Decimal(orange_weight),
        pay_type=kw.get("pay_type", "UPI"),
        goods_name=kw.get("goods_name", "orange juice"),
        cup_num=kw.get("cup_num", 1),
        createtime=kw.get("createtime", 0),
        order_date=kw.get("order_date", "2026-08-23"),
        raw={},
    )


def test_filter_requires_both_completed_and_success():
    assert passes_summary_filter(_order(order_status="Completed", delivery_status="Success")) is True
    assert passes_summary_filter(_order(order_status="Completed", delivery_status="Failure")) is False
    assert passes_summary_filter(_order(order_status="Cancel", delivery_status="Success")) is False
    assert passes_summary_filter(_order(order_status="Cancel", delivery_status="Failure")) is False


def test_non_qualifying_orders_excluded_from_summary():
    orders = [
        _order(device_app="NEXUS", order_money="120.00"),
        _order(device_app="NEXUS", order_status="Cancel", delivery_status="Failure", order_money="999.00"),
    ]
    rows = compute_daily_summary("2026-08-23", orders)
    all_row = next(r for r in rows if r.device_app == ALL_DEVICES_LABEL)
    assert all_row.number_of_orders == 1
    assert all_row.average_price == Decimal("120.00")


def test_aggregate_and_machine_wise_rows_both_present():
    orders = [
        _order(device_app="NEXUS", order_money="120.00", orange_num=3, orange_weight="200"),
        _order(device_app="NEXUS", order_money="120.00", orange_num=3, orange_weight="220"),
        _order(device_app="Gravity", order_money="100.00", orange_num=2, orange_weight="150"),
    ]
    rows = compute_daily_summary("2026-08-23", orders)
    by_device = {r.device_app: r for r in rows}

    assert set(by_device) == {ALL_DEVICES_LABEL, "NEXUS", "Gravity"}

    # NEXUS: 2 orders, avg price (120+120)/2=120.00, total oranges 6, avg juice weight (200+220)/2=210.00
    nexus = by_device["NEXUS"]
    assert nexus.number_of_orders == 2
    assert nexus.average_price == Decimal("120.00")
    assert nexus.total_number_of_oranges == 6
    assert nexus.average_juice_weight == Decimal("210.00")

    # Gravity: 1 order
    gravity = by_device["Gravity"]
    assert gravity.number_of_orders == 1
    assert gravity.average_price == Decimal("100.00")
    assert gravity.total_number_of_oranges == 2
    assert gravity.average_juice_weight == Decimal("150.00")

    # ALL: 3 orders combined, avg price (120+120+100)/3=113.33, total oranges 8, avg juice weight (200+220+150)/3=190.00
    all_row = by_device[ALL_DEVICES_LABEL]
    assert all_row.number_of_orders == 3
    assert all_row.average_price == Decimal("113.33")
    assert all_row.total_number_of_oranges == 8
    assert all_row.average_juice_weight == Decimal("190.00")


def test_zero_qualifying_orders_is_a_valid_quiet_day_not_an_error():
    orders = [_order(order_status="Cancel", delivery_status="Failure")]
    rows = compute_daily_summary("2026-08-23", orders)
    assert len(rows) == 1  # only the ALL row, no per-device rows
    assert rows[0].device_app == ALL_DEVICES_LABEL
    assert rows[0].number_of_orders == 0
    assert rows[0].average_price == Decimal("0.00")


def test_empty_input_list_is_a_valid_quiet_day():
    rows = compute_daily_summary("2026-08-23", [])
    assert len(rows) == 1
    assert rows[0].number_of_orders == 0


def test_device_wise_rows_sorted_alphabetically():
    orders = [_order(device_app="Zeta"), _order(device_app="Alpha")]
    rows = compute_daily_summary("2026-08-23", orders)
    device_rows = [r.device_app for r in rows if r.device_app != ALL_DEVICES_LABEL]
    assert device_rows == ["Alpha", "Zeta"]
