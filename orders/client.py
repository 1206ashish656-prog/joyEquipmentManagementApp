"""
OrdersClient: fetches Order Management -> Order Information data for a
single calendar day, using the target's own server-side date-range filter
(see selectors.py — confirmed live, UTC+8 day boundaries). Same
no-browser httpx approach as monitoring/lightweight_client.py, reusing
its cookie-loading/session-check logic rather than duplicating it —
orders are a second, independent read against the same authenticated
session, not a separate login.
"""
from __future__ import annotations

import json
import logging

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

    async def fetch_day(self, date_str: str) -> list[OrderRecord]:
        """date_str: 'YYYY-MM-DD'. Fetches every order the target reports
        for that UTC+8 calendar day (paginated), across all equipment —
        grouping by device happens later, in orders/summary.py."""
        next_day = _next_date_str(date_str)
        filt = json.dumps({CREATETIME_FILTER_FIELD: f"{date_str} - {next_day}"})
        op = json.dumps({CREATETIME_FILTER_FIELD: CREATETIME_FILTER_OP})

        limit = ORDER_LIST_PAGE_SIZE
        offset = 0
        all_records: list[OrderRecord] = []
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
            all_records.extend(map_api_row_to_order(r) for r in rows)

            offset += limit
            if not rows or (reported_total is not None and offset >= reported_total):
                break
            page_num += 1
            if page_num > _MAX_PAGINATION_PAGES:
                logger.warning("Hit pagination sanity guard (%s pages) for %s — stopping", _MAX_PAGINATION_PAGES, date_str)
                break

        if reported_total is not None and len(all_records) != reported_total:
            # Defensive check, not fatal — a day boundary the filter and our
            # pagination disagree on would silently under/over-count.
            logger.warning(
                "Order count mismatch for %s: API reported total=%s, fetched=%s",
                date_str, reported_total, len(all_records),
            )

        # Extra sanity check: every row's own computed order_date should
        # match what we asked for (requirement #28-style principle applied
        # here: a boundary/timezone bug should be visible, not silently
        # absorbed into the wrong day's numbers).
        mismatched = [r for r in all_records if r.order_date and r.order_date != date_str]
        if mismatched:
            logger.warning(
                "%d order(s) fetched for %s have a different computed order_date (%s) — "
                "possible day-boundary/timezone issue",
                len(mismatched), date_str, sorted({r.order_date for r in mismatched}),
            )

        return all_records

    async def close(self) -> None:
        await self._http.aclose()


def _next_date_str(date_str: str) -> str:
    from datetime import datetime, timedelta

    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")
