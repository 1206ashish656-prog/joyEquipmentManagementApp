"""
Unit tests for orders/summary.py — the filter (order_status=Completed AND
delivery_status=Success) and the (device_app, price, pay_type) grouping,
against known hand-computed expected values. Specifically covers the
explicit requirement: a mid-day price change on one machine produces two
separate groups for that machine, not one blended average.
"""
from __future__ import annotations

from decimal import Decimal

from orders.mapping import OrderRecord
from orders.summary import compute_daily_groups, passes_summary_filter


def _order(device_app="NEXUS", order_status="Completed", delivery_status="Success",
           order_money="120.00", orange_num=3, orange_weight="200", pay_type="UPI", **kw):
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
        pay_type=pay_type,
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


def test_non_qualifying_orders_excluded():
    orders = [
        _order(device_app="NEXUS", order_money="120.00"),
        _order(device_app="NEXUS", order_status="Cancel", delivery_status="Failure", order_money="999.00"),
    ]
    groups = compute_daily_groups("2026-08-23", orders)
    assert len(groups) == 1
    assert groups[0].number_of_orders == 1
    assert groups[0].price == Decimal("999.00") or groups[0].price == Decimal("120.00")  # only the qualifying one
    assert groups[0].price == Decimal("120.00")


def test_mid_day_price_change_produces_two_groups_for_same_machine():
    """The explicit requirement this feature exists to satisfy: 3 orders
    at 120.00 and 2 at 150.00 on NEXUS -> two separate NEXUS rows, not one
    blended average of 132.00."""
    orders = (
        [_order(device_app="NEXUS", order_money="120.00") for _ in range(3)]
        + [_order(device_app="NEXUS", order_money="150.00") for _ in range(2)]
    )
    groups = compute_daily_groups("2026-08-23", orders)
    assert len(groups) == 2

    by_price = {g.price: g for g in groups}
    assert set(by_price) == {Decimal("120.00"), Decimal("150.00")}
    assert by_price[Decimal("120.00")].number_of_orders == 3
    assert by_price[Decimal("150.00")].number_of_orders == 2
    # Critically: no group has a blended/averaged price.
    for g in groups:
        assert g.device_app == "NEXUS"


def test_grouping_also_splits_by_pay_type():
    orders = [
        _order(device_app="NEXUS", order_money="120.00", pay_type="UPI"),
        _order(device_app="NEXUS", order_money="120.00", pay_type="Cash"),
    ]
    groups = compute_daily_groups("2026-08-23", orders)
    assert len(groups) == 2
    pay_types = {g.pay_type for g in groups}
    assert pay_types == {"UPI", "Cash"}


def test_machine_wise_grouping():
    orders = [
        _order(device_app="NEXUS", order_money="120.00", orange_num=3, orange_weight="200"),
        _order(device_app="NEXUS", order_money="120.00", orange_num=3, orange_weight="220"),
        _order(device_app="Gravity", order_money="100.00", orange_num=2, orange_weight="150"),
    ]
    groups = compute_daily_groups("2026-08-23", orders)
    by_device = {g.device_app: g for g in groups}
    assert set(by_device) == {"NEXUS", "Gravity"}

    nexus = by_device["NEXUS"]
    assert nexus.number_of_orders == 2
    assert nexus.price == Decimal("120.00")
    assert nexus.total_number_of_oranges == 6
    assert nexus.average_juice_weight == Decimal("210.00")

    gravity = by_device["Gravity"]
    assert gravity.number_of_orders == 1
    assert gravity.total_number_of_oranges == 2
    assert gravity.average_juice_weight == Decimal("150.00")


def test_zero_qualifying_orders_returns_empty_list_not_error():
    orders = [_order(order_status="Cancel", delivery_status="Failure")]
    groups = compute_daily_groups("2026-08-23", orders)
    assert groups == []


def test_empty_input_returns_empty_list():
    assert compute_daily_groups("2026-08-23", []) == []


def test_groups_sorted_deterministically():
    orders = [
        _order(device_app="Zeta", order_money="100.00"),
        _order(device_app="Alpha", order_money="200.00"),
        _order(device_app="Alpha", order_money="100.00"),
    ]
    groups = compute_daily_groups("2026-08-23", orders)
    keys = [(g.device_app, g.price) for g in groups]
    assert keys == sorted(keys)
