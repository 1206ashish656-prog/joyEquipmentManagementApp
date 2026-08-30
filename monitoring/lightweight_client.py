"""
LightweightTargetClient: fetches equipment data with a plain async HTTP
client (httpx) and the cookies from a previously-saved Playwright
storage_state — NO browser/Chromium process involved at all.

Why this exists: the JSON list API (see selectors.py / mapping.py) never
needed a rendering engine — TargetApplicationClient was already fetching
it via `page.request.get()`, which is just an HTTP call that happens to
reuse the browser's cookie jar. Once a session is established, browser
automation isn't buying anything for the recurring poll itself; it's only
genuinely needed for the login/CAPTCHA step (human-in-the-loop,
requirement #3), which the code never asks this class to do — see
monitoring/worker.py's `_reauthenticate()`, which launches a real
Playwright browser ONLY for that, then closes it immediately.

Limitations vs. TargetApplicationClient (by design, not oversight):
  - No DOM-scraping fallback. If the JSON API's shape changes, this
    client raises ExtractionError like normal (never a fake equipment
    fault) — recovering would mean running the Playwright-based client
    (target_client.py) instead, which still has that fallback.

is_session_valid() probes the SAME JSON list endpoint get_equipment_data()
uses (limit=1), not the outer dashboard shell page. That distinction is
not cosmetic: this target renders its dashboard shell with HTTP 200 and
no redirect even with zero cookies at all (confirmed live — see git
history), so a URL/status-only check on that page is a false-positive
trap for "no session yet". The list API itself, however, reliably
responds with a JSON error payload (`{"code":0,"url":"...login...",
"wait":N}` — a standard FastAdmin "please log in" shape) when
unauthenticated, and `{"total":N,"rows":[...]}` when it isn't — so
checking the actual endpoint we depend on is both simpler and correct.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import Settings
from .fault_codes import describe_fault_code
from .mapping import _MAX_PAGINATION_PAGES, map_api_row_to_canonical
from .models import AuthenticationRequiredError, ExtractionError, TargetUnavailableError
from .selectors import (
    DEVICE_FAULT_LOG_API_PAGE_SIZE,
    DEVICE_FAULT_LOG_API_PATH,
    DEVICE_LIST_API_PAGE_SIZE,
    DEVICE_LIST_API_PATH,
)

logger = logging.getLogger(__name__)

# A realistic desktop Chrome UA — the target's JSON endpoint already
# responds correctly to a plain XHR-style request (confirmed via
# page.request.get() during discovery), but a normal-looking UA reduces
# the odds of being treated differently by any UA-sniffing middleware.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def is_not_authenticated_payload(data: object) -> bool:
    """True for FastAdmin's standard "please log in" AJAX response shape:
    {"code":0, "msg":"...", "url":"/.../index/login?...", "wait":N}"""
    return (
        isinstance(data, dict)
        and isinstance(data.get("url"), str)
        and "login" in data["url"].lower()
    )


def load_cookies(storage_state_path: Path) -> httpx.Cookies:
    cookies = httpx.Cookies()
    if not storage_state_path.exists():
        return cookies
    try:
        state = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read/parse storage state at %s — starting with no cookies", storage_state_path)
        return cookies
    for c in state.get("cookies", []):
        try:
            cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        except KeyError:
            continue
    return cookies


class LightweightTargetClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._device_list_api_url = f"{settings.target_base_url}{DEVICE_LIST_API_PATH}"
        self._http: httpx.AsyncClient | None = None
        self.reload_cookies()

    def reload_cookies(self) -> None:
        """Re-reads data/storage_state/session.json from disk — call this
        after a browser-based re-authentication (monitoring/worker.py's
        _reauthenticate()) has written a fresh session there."""
        cookies = load_cookies(self.settings.storage_state_path)
        self._http = httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": USER_AGENT},
        )

    def has_saved_session(self) -> bool:
        return self.settings.storage_state_path.exists()

    async def is_session_valid(self) -> bool:
        """Probes the actual list API (limit=1) rather than the dashboard
        shell page — see module docstring for why that distinction matters
        here."""
        assert self._http is not None
        try:
            resp = await self._http.get(
                self._device_list_api_url,
                params={"addtabs": "1", "sort": "id", "order": "desc", "offset": "0", "limit": "1", "filter": "{}", "op": "{}"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach target application: {e}") from e

        if resp.status_code >= 400:
            logger.warning("Session probe returned HTTP %s — treating session as invalid", resp.status_code)
            return False

        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.warning("Session probe did not return JSON — treating session as invalid")
            return False

        if isinstance(data, dict) and "rows" in data:
            return True
        if is_not_authenticated_payload(data):
            logger.info("Target reports not authenticated — session invalid, missing, or expired")
            return False

        logger.warning(
            "Unrecognized session-probe response shape — treating session as invalid. Keys: %s",
            list(data.keys()) if isinstance(data, dict) else type(data),
        )
        return False

    async def get_equipment_data(self) -> list[dict]:
        limit = DEVICE_LIST_API_PAGE_SIZE
        offset = 0
        all_rows: list[dict] = []
        page_num = 1
        assert self._http is not None

        while True:
            params = {
                "addtabs": "1",
                "sort": "id",
                "order": "desc",
                "offset": str(offset),
                "limit": str(limit),
                "filter": "{}",
                "op": "{}",
            }
            try:
                resp = await self._http.get(
                    self._device_list_api_url, params=params, headers={"X-Requested-With": "XMLHttpRequest"}
                )
            except httpx.HTTPError as e:
                raise TargetUnavailableError(f"Could not reach device list API: {e}") from e

            if resp.status_code != 200:
                raise ExtractionError(f"Device list API returned HTTP {resp.status_code}")

            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ExtractionError(f"Device list API did not return valid JSON: {e}") from e

            if is_not_authenticated_payload(data):
                # Session expired between is_session_valid() and this call
                # (or that check was skipped) — surfaced distinctly so the
                # worker retriggers re-authentication next cycle, rather
                # than a generic/opaque extraction failure.
                raise AuthenticationRequiredError(
                    "Device list API reports not authenticated mid-fetch — session expired."
                )

            if not isinstance(data, dict) or "rows" not in data:
                raise ExtractionError(
                    f"Device list API response missing 'rows' — schema may have changed. "
                    f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )

            rows = data["rows"]
            all_rows.extend(map_api_row_to_canonical(r) for r in rows)

            total = data.get("total", len(all_rows))
            offset += limit
            if not rows or offset >= total:
                break
            page_num += 1
            if page_num > _MAX_PAGINATION_PAGES:
                logger.warning("Hit API pagination sanity guard (%s pages) — stopping", _MAX_PAGINATION_PAGES)
                break

        return all_rows

    async def get_active_fault_log(self, device_id: str) -> list[dict]:
        """Fetches the currently-active (not yet auto-cleared, is_clean=0)
        rows from the target's Equipment Management > Fault Information
        tab for one device -- see selectors.DEVICE_FAULT_LOG_API_PATH.

        Deliberately best-effort: called from monitoring/worker.py only
        AFTER a FaultIncident is already persisted, purely to attach richer
        detail (monitoring/fault_codes.py) -- any exception here must be
        caught by the caller and must never block incident detection or
        the alert email itself. Returns canonical dicts with keys
        target_log_id/component_code/component_description/is_stop/
        is_clean/occurred_at/cleared_at -- occurred_at/cleared_at are
        aware UTC datetimes (the target's own timestamps are plain unix
        epoch, so no timezone-offset guessing is needed here, unlike the
        orders RANGE-filter day-boundary case in orders/mapping.py)."""
        assert self._http is not None
        url = f"{self.settings.target_base_url}{DEVICE_FAULT_LOG_API_PATH}"
        params = {
            "sort": "id",
            "order": "desc",
            "offset": "0",
            "limit": str(DEVICE_FAULT_LOG_API_PAGE_SIZE),
            "filter": json.dumps({"device_id": str(device_id), "is_clean": "0"}),
            "op": json.dumps({"device_id": "=", "is_clean": "="}),
        }
        try:
            resp = await self._http.get(url, params=params, headers={"X-Requested-With": "XMLHttpRequest"})
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach fault log API: {e}") from e

        if resp.status_code != 200:
            raise ExtractionError(f"Fault log API returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ExtractionError(f"Fault log API did not return valid JSON: {e}") from e

        if is_not_authenticated_payload(data):
            raise AuthenticationRequiredError("Fault log API reports not authenticated mid-fetch — session expired.")

        if not isinstance(data, dict) or "rows" not in data:
            raise ExtractionError(
                f"Fault log API response missing 'rows' — schema may have changed. "
                f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

        return [map_fault_log_row_to_canonical(r) for r in data["rows"]]

    async def get_fault_log_history(self, device_id: str) -> list[dict]:
        """Fetches EVERY historical row (regardless of is_clean status)
        from the target's Fault Information tab for one device, paginated
        exactly like get_equipment_data() -- for the one-time/re-runnable
        backfill (monitoring/fault_log_backfill.py) into FaultLogHistory,
        the sole source for the Senior Management Report's downtime
        section. Unlike get_active_fault_log() (best-effort, real-time
        alert enrichment), a failure here should stop the backfill run
        rather than be silently swallowed -- the caller doesn't catch
        these the way monitoring/worker.py does."""
        assert self._http is not None
        url = f"{self.settings.target_base_url}{DEVICE_FAULT_LOG_API_PATH}"
        limit = DEVICE_FAULT_LOG_API_PAGE_SIZE
        offset = 0
        all_rows: list[dict] = []
        page_num = 1

        while True:
            params = {
                "sort": "id",
                "order": "desc",
                "offset": str(offset),
                "limit": str(limit),
                "filter": json.dumps({"device_id": str(device_id)}),
                "op": json.dumps({"device_id": "="}),
            }
            try:
                resp = await self._http.get(url, params=params, headers={"X-Requested-With": "XMLHttpRequest"})
            except httpx.HTTPError as e:
                raise TargetUnavailableError(f"Could not reach fault log API: {e}") from e

            if resp.status_code != 200:
                raise ExtractionError(f"Fault log API returned HTTP {resp.status_code}")

            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ExtractionError(f"Fault log API did not return valid JSON: {e}") from e

            if is_not_authenticated_payload(data):
                raise AuthenticationRequiredError(
                    "Fault log API reports not authenticated mid-fetch — session expired."
                )

            if not isinstance(data, dict) or "rows" not in data:
                raise ExtractionError(
                    f"Fault log API response missing 'rows' — schema may have changed. "
                    f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )

            rows = data["rows"]
            all_rows.extend(map_fault_log_row_to_canonical(r) for r in rows)

            total = data.get("total", len(all_rows))
            offset += limit
            if not rows or offset >= total:
                break
            page_num += 1
            if page_num > _MAX_PAGINATION_PAGES:
                logger.warning("Hit fault log pagination sanity guard (%s pages) — stopping", _MAX_PAGINATION_PAGES)
                break

        return all_rows

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def map_fault_log_row_to_canonical(row: dict) -> dict:
    """Maps one raw device_fault_log API row to the canonical shape
    db.models.FaultLogEntry expects. Never raises on a malformed epoch --
    falls back to "now" rather than dropping the row, since a fault log
    entry with a slightly-off timestamp is still far more useful than
    silently losing it (requirement #28's spirit: don't let a formatting
    hiccup destroy real information)."""

    def _epoch_to_utc(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    code = str(row.get("code") or "")
    return {
        "target_log_id": int(row.get("id") or 0),
        "component_code": code,
        "component_description": describe_fault_code(code),
        "is_stop": bool(row.get("is_stop")),
        "is_clean": bool(row.get("is_clean")),
        "occurred_at": _epoch_to_utc(row.get("createtime")) or datetime.now(timezone.utc),
        "cleared_at": _epoch_to_utc(row.get("clean_time")),
    }
