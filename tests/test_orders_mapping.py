"""
Unit tests for orders/mapping.py — field mapping and the UTC+8 day
computation (the single most important thing to get right here: getting
the timezone wrong silently misassigns orders to the wrong day).
"""
from __future__ import annotations

from decimal import Decimal

from orders.mapping import map_api_row_to_order


def _raw_row(**overrides):
    row = {
        "id": 225226,
        "order_code": "2026082451100531",
        "device_id": 205,
        "order_money": "120.00",
        "createtime": 1787505619,  # confirmed live: 2026-08-24 01:20:19 UTC+8
        "pay_state_text": "Have paid",
        "delivery_state_text": "Success",
        "order_state_text": "Completed",
        "pay_type_text": "UPI",
        "goods_name": "orange juice",
        "cup_num": "1",
        "orange_num": "3",
        "orange_weight": "207",
        "device": {"name": "NEXUS"},
    }
    row.update(overrides)
    return row


def test_maps_basic_fields():
    order = map_api_row_to_order(_raw_row())
    assert order.order_id == "225226"
    assert order.order_code == "2026082451100531"
    assert order.device_app == "NEXUS"
    assert order.order_status == "Completed"
    assert order.payment_status == "Have paid"
    assert order.delivery_status == "Success"
    assert order.order_money == Decimal("120.00")
    assert order.orange_num == 3
    assert order.orange_weight == Decimal("207")
    assert order.pay_type == "UPI"


def test_order_date_uses_utc8_not_utc():
    # 1787505619 is 2026-08-23 17:20:19 UTC, but 2026-08-24 01:20:19 in
    # UTC+8 -- confirmed against the real target's own day-boundary
    # behavior. A naive UTC (or local-machine-time) conversion would land
    # this order in the wrong day.
    order = map_api_row_to_order(_raw_row(createtime=1787505619))
    assert order.order_date == "2026-08-24"


def test_order_date_just_before_utc8_midnight():
    # 1787500149 confirmed live as 2026-08-23 23:49:09 UTC+8 (the last
    # order of that day in the real capture).
    order = map_api_row_to_order(_raw_row(createtime=1787500149))
    assert order.order_date == "2026-08-23"


def test_missing_device_name_defaults_to_nexus():
    # Confirmed with the account owner: every raw order row with a null
    # device.name is Nexus machine data, not an unidentifiable source.
    order = map_api_row_to_order(_raw_row(device={}))
    assert order.device_app == "NEXUS"


def test_malformed_money_defaults_to_zero_not_crash():
    order = map_api_row_to_order(_raw_row(order_money="not-a-number"))
    assert order.order_money == Decimal("0")


def test_raw_preserved_for_audit():
    raw = _raw_row()
    order = map_api_row_to_order(raw)
    assert order.raw is raw
