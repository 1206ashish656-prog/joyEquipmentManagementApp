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
  - is_session_valid() checks the redirect target and response status
    only (no page rendering, no "Console" text visibility check) — a
    plain HTTP GET can't evaluate JS-rendered content anyway. This is the
    same signal (redirect to /login = invalid) requirement #13 asks for.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .config import Settings
from .mapping import _MAX_PAGINATION_PAGES, map_api_row_to_canonical
from .models import ExtractionError, TargetUnavailableError
from .selectors import DEVICE_LIST_API_PAGE_SIZE, DEVICE_LIST_API_PATH

logger = logging.getLogger(__name__)

# A realistic desktop Chrome UA — the target's JSON endpoint already
# responds correctly to a plain XHR-style request (confirmed via
# page.request.get() during discovery), but a normal-looking UA reduces
# the odds of being treated differently by any UA-sniffing middleware.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def _derive_dashboard_url(settings: Settings) -> str:
    try:
        parsed = urlparse(settings.target_login_url)
        qs = parse_qs(parsed.query)
        target_path = unquote(qs["url"][0])
        return f"{parsed.scheme}://{parsed.netloc}{target_path}"
    except Exception:
        return settings.target_base_url


def _load_cookies(storage_state_path: Path) -> httpx.Cookies:
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
        self._dashboard_url = _derive_dashboard_url(settings)
        self._device_list_api_url = f"{settings.target_base_url}{DEVICE_LIST_API_PATH}"
        self._http: httpx.AsyncClient | None = None
        self.reload_cookies()

    def reload_cookies(self) -> None:
        """Re-reads data/storage_state/session.json from disk — call this
        after a browser-based re-authentication (monitoring/worker.py's
        _reauthenticate()) has written a fresh session there."""
        cookies = _load_cookies(self.settings.storage_state_path)
        self._http = httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": _USER_AGENT},
        )

    def has_saved_session(self) -> bool:
        return self.settings.storage_state_path.exists()

    async def is_session_valid(self) -> bool:
        assert self._http is not None
        try:
            resp = await self._http.get(self._dashboard_url)
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach target application: {e}") from e

        final_url = str(resp.url).lower()
        if "login" in final_url:
            logger.info("Redirected to login page — session invalid or expired")
            return False
        if resp.status_code >= 400:
            logger.warning("Dashboard probe returned HTTP %s — treating session as invalid", resp.status_code)
            return False
        return True

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

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
