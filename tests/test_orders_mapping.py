"""
Unit tests for orders/mapping.py — field mapping and the IST (India
Standard Time) day computation (the single most important thing to get
right here: getting the timezone wrong silently misassigns orders to the
wrong day). Per explicit request (2026-08-24), order_date is computed in
IST, NOT the target's own UTC+8 (China Standard Time) day boundary —
that UTC+8 fact is still real and still relied on by orders/client.py to
query the target correctly, it's just no longer what gets reported.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from orders.mapping import format_ist, map_api_row_to_order


def _raw_row(**overrides):
    row = {
        "id": 225226,
        "order_code": "2026082451100531",
        "device_id": 205,
        "order_money": "120.00",
        "createtime": 1787505619,  # confirmed live: 2026-08-24 01:20:19 UTC+8 = 2026-08-23 22:50:19 IST
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


def test_order_date_uses_ist_not_utc():
    # 1787505619 is 2026-08-23 17:20:19 UTC, which is 2026-08-23 22:50:19
    # in IST -- a naive UTC (or local-machine-time) conversion assuming
    # UTC+8 semantics would land this order on 2026-08-24 instead.
    order = map_api_row_to_order(_raw_row(createtime=1787505619))
    assert order.order_date == "2026-08-23"


def test_order_date_diverges_from_target_utc8_day():
    # The exact same instant is 2026-08-24 01:20:19 in the TARGET's own
    # UTC+8 day-boundary convention (confirmed live -- see
    # orders/selectors.py), but 2026-08-23 in IST. This is the deliberate
    # divergence orders/client.py's fetch_day() has to reconcile by
    # querying two of the target's UTC+8 days per IST day.
    order = map_api_row_to_order(_raw_row(createtime=1787505619))
    assert order.order_date == "2026-08-23"  # NOT "2026-08-24" (that would be the UTC+8 answer)


def test_order_date_just_before_ist_midnight():
    # 1787509795 = 2026-08-23 23:59:55 IST exactly.
    order = map_api_row_to_order(_raw_row(createtime=1787509795))
    assert order.order_date == "2026-08-23"


def test_order_date_just_after_ist_midnight():
    # 1787509805 = 2026-08-24 00:00:05 IST exactly -- 10 seconds later
    # than the previous test's timestamp, but the next calendar day.
    order = map_api_row_to_order(_raw_row(createtime=1787509805))
    assert order.order_date == "2026-08-24"


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


# --- format_ist() -- shared display helper (backend/templating.py's
# `ist` Jinja filter and services/fault_digest.py's email tables both
# use this same implementation) ---

def test_format_ist_converts_utc_to_ist():
    # 2026-08-25 18:30:00 UTC = 2026-08-26 00:00:00 IST (UTC+5:30).
    value = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert format_ist(value) == "26 Aug 2026, 00:00:00 IST"


def test_format_ist_handles_none():
    assert format_ist(None) == "—"


def test_format_ist_assumes_naive_datetime_is_utc():
    # SQLite can round-trip a tz-aware column back as naive -- must be
    # treated as UTC (the only thing db/models.py's _utcnow() ever
    # produces), not left ambiguous or misinterpreted as local time.
    naive = datetime(2026, 8, 25, 18, 30, 0)
    aware = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert format_ist(naive) == format_ist(aware)


def test_format_ist_custom_format():
    value = datetime(2026, 8, 25, 18, 30, 0, tzinfo=timezone.utc)
    assert format_ist(value, fmt="%Y-%m-%d") == "2026-08-26"
