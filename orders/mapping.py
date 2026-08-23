"""
Raw order-list API row -> canonical OrderRecord. Kept separate from
services/health_engine.py's world on purpose: orders are a reporting
concern (daily business summary), not equipment health monitoring, even
though they're fetched from the same target application/session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .selectors import FIELD_MAP

# Confirmed live (see selectors.py docstring): the target's day-boundary
# filter operates in China Standard Time, not UTC/IST/local time.
TARGET_TZ = timezone(timedelta(hours=8), name="UTC+8 (target server)")


def _get_nested(d: dict, dotted_path: str, default=None):
    cur: Any = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _to_decimal(value, default=Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def _to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class OrderRecord:
    order_id: str
    order_code: str
    device_id: str
    device_app: str  # equipment name, e.g. "NEXUS" — the summary grouping key
    order_status: str  # order_state_text: e.g. "Completed" / "Cancel"
    payment_status: str  # pay_state_text: e.g. "Have paid" / "Non-payment"
    delivery_status: str  # delivery_state_text: e.g. "Success" / "Failure"
    order_money: Decimal  # price for this order
    orange_num: int  # oranges used for this order
    orange_weight: Decimal  # juice weight for this order (grams, per live data)
    pay_type: str
    goods_name: str
    cup_num: int
    createtime: int  # raw unix timestamp, as returned by the target
    order_date: str  # YYYY-MM-DD, computed in TARGET_TZ (UTC+8) — see selectors.py
    raw: dict = field(default_factory=dict)


def map_api_row_to_order(row: dict) -> OrderRecord:
    mapped: dict[str, Any] = {}
    for api_field, our_field in FIELD_MAP.items():
        mapped[our_field] = _get_nested(row, api_field)

    createtime = _to_int(mapped.get("createtime"))
    order_date = (
        datetime.fromtimestamp(createtime, tz=timezone.utc).astimezone(TARGET_TZ).strftime("%Y-%m-%d")
        if createtime
        else ""
    )

    return OrderRecord(
        order_id=str(mapped.get("order_id") or ""),
        order_code=str(mapped.get("order_code") or ""),
        device_id=str(mapped.get("device_id") or ""),
        device_app=str(mapped.get("device_app") or "UNKNOWN"),
        order_status=str(mapped.get("order_status") or "").strip(),
        payment_status=str(mapped.get("payment_status") or "").strip(),
        delivery_status=str(mapped.get("delivery_status") or "").strip(),
        order_money=_to_decimal(mapped.get("order_money")),
        orange_num=_to_int(mapped.get("orange_num")),
        orange_weight=_to_decimal(mapped.get("orange_weight")),
        pay_type=str(mapped.get("pay_type") or "").strip(),
        goods_name=str(mapped.get("goods_name") or "").strip(),
        cup_num=_to_int(mapped.get("cup_num")),
        createtime=createtime,
        order_date=order_date,
        raw=row,
    )
