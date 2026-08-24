"""
Unit tests for orders/client.py's fetch_day() — specifically the IST-day
reconciliation: the target's own RANGE filter buckets by ITS OWN UTC+8
calendar day, so one IST calendar day always straddles two of the
target's UTC+8 days. No real network call is made — httpx.AsyncClient's
transport is swapped for httpx.MockTransport (same pattern as
test_lightweight_client.py).
"""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from monitoring.config import load_settings
from orders.client import OrdersClient

# Four createtime values chosen to land in each quadrant of the two
# target-side (UTC+8) days that make up IST day 2026-08-24:
#   - target UTC+8 day 2026-08-24, early hours   -> IST 2026-08-23 (excluded: spilled BACK a day)
#   - target UTC+8 day 2026-08-24, midday        -> IST 2026-08-24 (included)
#   - target UTC+8 day 2026-08-25, early hours   -> IST 2026-08-24 (included: spilled FORWARD a day)
#   - target UTC+8 day 2026-08-25, midday        -> IST 2026-08-25 (excluded)
CT_DAY1_EARLY_SPILLBACK = 1787504400  # UTC+8 2026-08-24 01:00 -> IST 2026-08-23
CT_DAY1_MIDDAY = 1787544000  # UTC+8 2026-08-24 12:00 -> IST 2026-08-24
CT_DAY2_EARLY_SPILLFORWARD = 1787590800  # UTC+8 2026-08-25 01:00 -> IST 2026-08-24
CT_DAY2_MIDDAY = 1787630400  # UTC+8 2026-08-25 12:00 -> IST 2026-08-25


def _settings(tmp_path, **overrides):
    base = load_settings()
    storage_state_path = tmp_path / "session.json"
    return replace(base, storage_state_path=storage_state_path, **overrides)


def _row(order_id: str, createtime: int) -> dict:
    return {
        "id": order_id, "order_code": f"C{order_id}", "device_id": 205, "order_money": "120.00",
        "createtime": createtime, "pay_state_text": "Have paid", "delivery_state_text": "Success",
        "order_state_text": "Completed", "pay_type_text": "UPI", "goods_name": "orange juice",
        "cup_num": "1", "orange_num": "3", "orange_weight": "207", "device": {"name": "NEXUS"},
    }


def _target_date_queried(request: httpx.Request) -> str:
    """Pulls the target-side date out of the RANGE filter query param."""
    filt = json.loads(request.url.params["filter"])
    return filt["createtime"].split(" - ")[0]


@pytest.mark.asyncio
async def test_fetch_day_queries_both_target_side_utc8_days(tmp_path):
    queried_dates = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried_dates.append(_target_date_queried(request))
        return httpx.Response(200, json={"total": 0, "rows": []})

    client = OrdersClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.fetch_day("2026-08-24")

    assert queried_dates == ["2026-08-24", "2026-08-25"]  # IST day + its target-side successor


@pytest.mark.asyncio
async def test_fetch_day_keeps_only_orders_matching_the_ist_day(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        target_date = _target_date_queried(request)
        if target_date == "2026-08-24":
            rows = [_row("1", CT_DAY1_EARLY_SPILLBACK), _row("2", CT_DAY1_MIDDAY)]
        elif target_date == "2026-08-25":
            rows = [_row("3", CT_DAY2_EARLY_SPILLFORWARD), _row("4", CT_DAY2_MIDDAY)]
        else:
            rows = []
        return httpx.Response(200, json={"total": len(rows), "rows": rows})

    client = OrdersClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    records = await client.fetch_day("2026-08-24")

    # Only orders 2 (target day-1 midday) and 3 (target day-2 early
    # spillover) actually fall on IST 2026-08-24 -- 1 spilled back to
    # 08-23, 4 belongs to 08-25.
    assert {r.order_id for r in records} == {"2", "3"}
    assert all(r.order_date == "2026-08-24" for r in records)


@pytest.mark.asyncio
async def test_fetch_day_empty_when_nothing_matches(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        target_date = _target_date_queried(request)
        rows = [_row("1", CT_DAY1_EARLY_SPILLBACK)] if target_date == "2026-08-24" else []
        return httpx.Response(200, json={"total": len(rows), "rows": rows})

    client = OrdersClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    records = await client.fetch_day("2026-08-24")

    assert records == []  # the only row fetched belongs to IST 08-23, not the requested 08-24


@pytest.mark.asyncio
async def test_fetch_day_legitimate_spillover_does_not_warn(tmp_path, caplog):
    """CT_DAY1_EARLY_SPILLBACK and CT_DAY2_MIDDAY are both EXPECTED
    outcomes of querying two target-side UTC+8 days (they land on the
    IST day before / after the requested one, respectively) -- neither
    should trip the "unexpected order_date" sanity-check warning. A
    previous version of this logic got the expected-date window wrong
    (only looked forward, not backward) and warned on every single
    normal day."""
    def handler(request: httpx.Request) -> httpx.Response:
        target_date = _target_date_queried(request)
        if target_date == "2026-08-24":
            rows = [_row("1", CT_DAY1_EARLY_SPILLBACK)]
        elif target_date == "2026-08-25":
            rows = [_row("4", CT_DAY2_MIDDAY)]
        else:
            rows = []
        return httpx.Response(200, json={"total": len(rows), "rows": rows})

    client = OrdersClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await client.fetch_day("2026-08-24")

    assert "unexpected computed order_date" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_day_paginates_within_each_target_day(tmp_path):
    page1 = [_row(str(i), CT_DAY1_MIDDAY) for i in range(100)]
    page2 = [_row(str(i), CT_DAY1_MIDDAY) for i in range(100, 150)]

    def handler(request: httpx.Request) -> httpx.Response:
        target_date = _target_date_queried(request)
        if target_date != "2026-08-24":
            return httpx.Response(200, json={"total": 0, "rows": []})
        offset = int(request.url.params["offset"])
        if offset == 0:
            return httpx.Response(200, json={"total": 150, "rows": page1})
        return httpx.Response(200, json={"total": 150, "rows": page2})

    client = OrdersClient(_settings(tmp_path))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    records = await client.fetch_day("2026-08-24")

    assert len(records) == 150
