"""
OrdersClient: fetches Order Management -> Order Information data for a
single IST (India Standard Time) calendar day. Same no-browser httpx
approach as monitoring/lightweight_client.py, reusing its
cookie-loading/session-check logic rather than duplicating it — orders
are a second, independent read against the same authenticated session,
not a separate login.

IST vs. the target's own day boundary: the target's server-side RANGE
filter buckets orders by ITS OWN China Standard Time (UTC+8) calendar
day (confirmed live — see selectors.py), 2.5 hours ahead of IST. So one
IST calendar day always straddles parts of TWO of the target's UTC+8
days — fetch_day() below queries both, then keeps only the orders whose
own computed order_date (orders/mapping.py, IST-based) actually matches
the requested day. The target's RANGE filter is a coarse over-fetch, not
the final word on which day an order belongs to.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import httpx

from monitoring.config import Settings
from monitoring.lightweight_client import USER_AGENT, is_not_authenticated_payload, load_cookies
from monitoring.models import ExtractionError, TargetUnavailableError

from .mapping import OrderRecord, map_api_row_to_order
from .selectors import (
    CREATETIME_FILTER_FIELD,
    CREATETIME_FILTER_OP,
    ORDER_LIST_API_PATH,
    ORDER_LIST_PAGE_SIZE,
)

logger = logging.getLogger(__name__)

_MAX_PAGINATION_PAGES = 2000  # a day's orders shouldn't remotely approach this


class OrdersClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._order_list_api_url = f"{settings.target_base_url}{ORDER_LIST_API_PATH}"
        self._http = httpx.AsyncClient(
            cookies=load_cookies(settings.storage_state_path),
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": USER_AGENT},
        )

    async def fetch_day(self, ist_date_str: str) -> list[OrderRecord]:
        """ist_date_str: 'YYYY-MM-DD', an IST calendar day. Queries the
        target's own UTC+8-bucketed RANGE filter for BOTH of the target
        days that could contain an order landing on this IST day (see
        module docstring), merges, then keeps only the orders whose own
        computed order_date (IST-based) matches ist_date_str exactly —
        grouping by device happens later, in orders/summary.py."""
        target_date_1 = ist_date_str
        target_date_2 = _next_date_str(ist_date_str)

        all_records: list[OrderRecord] = []
        for target_date in (target_date_1, target_date_2):
            records = await self._fetch_target_utc8_day(target_date)
            all_records.extend(records)

        matching = [r for r in all_records if r.order_date == ist_date_str]

        # Sanity check, not fatal: this is the app-level boundary/timezone
        # principle applied here (a mistake here should be visible, not
        # silently absorbed into the wrong day's numbers). Target UTC+8
        # day D's orders legitimately land on IST day D-1 (the last ~2.5h
        # of D-1, per the module docstring) OR D — never further out — so
        # across target_date_1 (=D) and target_date_2 (=D+1), the full
        # legitimate spread is exactly {D-1, D, D+1}. Anything outside
        # that is unexpected.
        expected_dates = {_previous_date_str(target_date_1), target_date_1, target_date_2}
        unexpected = [r for r in all_records if r.order_date and r.order_date not in expected_dates]
        if unexpected:
            logger.warning(
                "%d order(s) fetched for IST day %s have an unexpected computed order_date (%s) — "
                "possible day-boundary/timezone issue",
                len(unexpected), ist_date_str, sorted({r.order_date for r in unexpected}),
            )

        return matching

    async def _fetch_target_utc8_day(self, target_date_str: str) -> list[OrderRecord]:
        """One of the target's OWN UTC+8 calendar days (paginated) — the
        raw building block fetch_day() combines two of, to cover a full
        IST day. Not filtered by order_date; that happens in fetch_day()."""
        next_day = _next_date_str(target_date_str)
        filt = json.dumps({CREATETIME_FILTER_FIELD: f"{target_date_str} - {next_day}"})
        op = json.dumps({CREATETIME_FILTER_FIELD: CREATETIME_FILTER_OP})

        limit = ORDER_LIST_PAGE_SIZE
        offset = 0
        records: list[OrderRecord] = []
        reported_total: int | None = None
        page_num = 1

        while True:
            params = {
                "addtabs": "1",
                "sort": "id",
                "order": "desc",
                "offset": str(offset),
                "limit": str(limit),
                "filter": filt,
                "op": op,
            }
            try:
                resp = await self._http.get(
                    self._order_list_api_url, params=params, headers={"X-Requested-With": "XMLHttpRequest"}
                )
            except httpx.HTTPError as e:
                raise TargetUnavailableError(f"Could not reach order list API: {e}") from e

            if resp.status_code != 200:
                raise ExtractionError(f"Order list API returned HTTP {resp.status_code}")

            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ExtractionError(f"Order list API did not return valid JSON: {e}") from e

            if is_not_authenticated_payload(data):
                from monitoring.models import AuthenticationRequiredError

                raise AuthenticationRequiredError("Order list API reports not authenticated mid-fetch.")

            if not isinstance(data, dict) or "rows" not in data:
                raise ExtractionError(
                    f"Order list API response missing 'rows' — schema may have changed. "
                    f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )

            reported_total = data.get("total", reported_total)
            rows = data["rows"]
            records.extend(map_api_row_to_order(r) for r in rows)

            offset += limit
            if not rows or (reported_total is not None and offset >= reported_total):
                break
            page_num += 1
            if page_num > _MAX_PAGINATION_PAGES:
                logger.warning("Hit pagination sanity guard (%s pages) for target day %s — stopping", _MAX_PAGINATION_PAGES, target_date_str)
                break

        if reported_total is not None and len(records) != reported_total:
            # Defensive check, not fatal — a day boundary the filter and our
            # pagination disagree on would silently under/over-count.
            logger.warning(
                "Order count mismatch for target day %s: API reported total=%s, fetched=%s",
                target_date_str, reported_total, len(records),
            )

        return records

    async def close(self) -> None:
        await self._http.aclose()


def _next_date_str(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def _previous_date_str(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d - timedelta(days=1)).strftime("%Y-%m-%d")
