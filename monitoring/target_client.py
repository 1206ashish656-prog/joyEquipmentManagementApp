"""
TargetApplicationClient: the ONLY module that talks to jwintell.com.

Everything else in the pipeline works with normalized data (see
equipment_extractor.py). If the target site's markup/API changes, this
file (plus selectors.py) is what should need updating — see requirement
#34.

See selectors.py's module docstring and
docs/target_application_integration_spec.md for how the target app is
structured (confirmed via discovery on 2026-08-23): equipment data is
retrieved primarily via its own JSON list API (requirement #15), with DOM
scraping of the same Bootstrap Table kept as a fallback.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import Settings
from .models import AuthenticationRequiredError, ExtractionError, TargetUnavailableError
from .selectors import (
    API_FIELD_MAP,
    AUTHENTICATED_MARKER_TEXT,
    COLUMN_HEADER_MAP,
    DEVICE_INFORMATION_PATH,
    DEVICE_LIST_API_PAGE_SIZE,
    DEVICE_LIST_API_PATH,
    DEVICE_TABLE_BODY_CELL,
    DEVICE_TABLE_BODY_ROW,
    DEVICE_TABLE_CONTAINER,
    DEVICE_TABLE_HEADER_CELL,
    DEVICE_TABLE_HEADER_ROW,
    LOGIN_PASSWORD_INPUT,
    LOGIN_USERNAME_INPUT,
    PAGINATION_NEXT_BUTTON,
)

logger = logging.getLogger(__name__)

_MAX_PAGINATION_PAGES = 500  # sanity guard, not an expected real value


def _derive_dashboard_url(settings: Settings) -> str:
    """The login URL embeds the post-login redirect target as its `url`
    query param — reuse it as the "home"/authenticated page we probe for
    session-validity checks, instead of guessing/hardcoding a separate URL."""
    try:
        parsed = urlparse(settings.target_login_url)
        qs = parse_qs(parsed.query)
        target_path = unquote(qs["url"][0])
        return f"{parsed.scheme}://{parsed.netloc}{target_path}"
    except Exception:
        logger.warning("Could not derive dashboard URL from TARGET_LOGIN_URL; falling back to base URL")
        return settings.target_base_url


def _get_nested(d: dict, dotted_path: str, default=None):
    cur = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _blank_canonical_row() -> dict:
    return {
        "equipment_id": "",
        "equipment_code": "",
        "name": "",
        "device_type": "",
        "status": "",
        "network_status": "",
        "fault_type": "",
        "material_shortage_status": "",
        "advertising_group": None,
        "device_address": None,
        "selling_price": None,
        "remaining_oranges": None,
        "_raw": {},
    }


def _map_api_row_to_canonical(row: dict) -> dict:
    """Maps one raw JSON API row to our canonical field names via
    selectors.API_FIELD_MAP, preserving the full original row as `_raw`
    for audit (requirement #10)."""
    canonical = _blank_canonical_row()
    for api_field, our_field in API_FIELD_MAP.items():
        value = _get_nested(row, api_field)
        if value is not None and isinstance(value, str):
            value = value.strip()
        canonical[our_field] = value if value is not None else canonical[our_field]
    canonical["equipment_id"] = str(canonical["equipment_id"] or "")
    canonical["_raw"] = row
    return canonical


def _map_dom_row_to_canonical(row: dict[str, str]) -> dict:
    """Maps one raw DOM-scraped {header_text: cell_text} row to our
    canonical field names via selectors.COLUMN_HEADER_MAP."""
    canonical = _blank_canonical_row()
    for raw_header, value in row.items():
        our_field = COLUMN_HEADER_MAP.get(raw_header.strip().lower())
        if our_field:
            canonical[our_field] = (value or "").strip()
    canonical["_raw"] = row
    return canonical


class TargetApplicationClient:
    def __init__(self, page: Page, settings: Settings):
        self.page = page
        self.settings = settings
        self._dashboard_url = _derive_dashboard_url(settings)
        self._device_info_url = f"{settings.target_base_url}{DEVICE_INFORMATION_PATH}?addtabs=1"
        self._device_list_api_url = f"{settings.target_base_url}{DEVICE_LIST_API_PATH}"

    async def is_session_valid(self) -> bool:
        """Navigates to the authenticated home page and checks whether the
        target redirected us to login. Per requirement #13: a redirect to
        login means the session is invalid — this must be detected before
        any scraping happens, so we never mistake login-page content for
        equipment data."""
        try:
            await self.page.goto(self._dashboard_url, wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError as e:
            raise TargetUnavailableError(f"Timed out reaching target application: {e}") from e
        except PlaywrightError as e:
            raise TargetUnavailableError(f"Could not reach target application: {e}") from e

        current_url = self.page.url.lower()
        if "login" in current_url:
            logger.info("Redirected to login page — session invalid or expired")
            return False

        try:
            marker = self.page.get_by_text(AUTHENTICATED_MARKER_TEXT, exact=False).first
            if not await marker.is_visible(timeout=5000):
                logger.warning(
                    "Authenticated marker %r not found — treating session as invalid "
                    "(update selectors.AUTHENTICATED_MARKER_TEXT once confirmed)",
                    AUTHENTICATED_MARKER_TEXT,
                )
                return False
        except PlaywrightError:
            return False

        return True

    async def authenticate(self) -> None:
        """Human-in-the-loop login. Per requirement #3, this deliberately
        does NOT attempt to solve the CAPTCHA — it prefills what it safely
        can and hands control to a person for the CAPTCHA + submit."""
        try:
            await self.page.goto(self.settings.target_login_url, wait_until="domcontentloaded", timeout=20000)
        except PlaywrightTimeoutError as e:
            raise TargetUnavailableError(f"Timed out reaching login page: {e}") from e
        except PlaywrightError as e:
            raise TargetUnavailableError(f"Could not reach login page: {e}") from e

        if self.settings.headless:
            raise AuthenticationRequiredError(
                "No valid session, and running headless: the CAPTCHA requires a "
                "human. Re-run with HEADLESS=false, complete login once, and the "
                "saved session can then be reused (including headlessly) until it "
                "expires."
            )

        if self.settings.has_credentials:
            try:
                await self.page.fill(LOGIN_USERNAME_INPUT, self.settings.target_username, timeout=5000)
                await self.page.fill(LOGIN_PASSWORD_INPUT, self.settings.target_password, timeout=5000)
                logger.info("Prefilled username/password. Please complete the CAPTCHA and submit.")
            except PlaywrightError:
                logger.info(
                    "Could not auto-fill credentials (selectors not confirmed yet — "
                    "see selectors.py TODO_DISCOVERY items). Please log in manually."
                )
        else:
            logger.info("TARGET_USERNAME/TARGET_PASSWORD not set — please log in manually.")

        print("\n" + "=" * 70)
        print("ACTION REQUIRED — Login page is open in the browser window.")
        print("Please complete the CAPTCHA and submit the login form yourself.")
        print("(CAPTCHA is never solved automatically by this application.)")
        print(f"Waiting up to {self.settings.auth_manual_timeout_seconds}s for you to finish...")
        print("=" * 70)

        # Poll page.url (a local property — does NOT navigate) rather than
        # blocking on console input(). This works whether a human is typing
        # into this process's own terminal or just watching/driving the
        # visible browser window directly (e.g. a monitoring worker running
        # as a service, or this process being driven by tooling rather than
        # an interactive shell). Deliberately does not call
        # is_session_valid() in the loop — that does a page.goto(), which
        # would yank the browser away from the login form mid-CAPTCHA-entry.
        deadline = asyncio.get_event_loop().time() + self.settings.auth_manual_timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if "login" not in self.page.url.lower():
                break
            await asyncio.sleep(2)
        else:
            raise AuthenticationRequiredError(
                f"Timed out after {self.settings.auth_manual_timeout_seconds}s waiting for "
                "manual login/CAPTCHA completion (still on the login page)."
            )

        if not await self.is_session_valid():
            raise AuthenticationRequiredError(
                "Still redirected to login after manual login attempt. Check "
                "credentials, or that the CAPTCHA/login actually completed."
            )
        logger.info("Authentication confirmed — session established.")

    async def open_device_information(self) -> None:
        """Navigates directly to the Device Information content URL.

        NOTE: earlier we tried clicking the sidebar ("Equipment Management"
        -> "Device Information") but the submenu is a slide-toggle that
        doesn't reliably respond to a plain Playwright click, leaving the
        target link present-but-hidden in the DOM. Since we know its real
        destination URL (confirmed via discovery), navigating straight
        there is both simpler and more reliable — see
        docs/target_application_integration_spec.md.
        """
        try:
            await self.page.goto(self._device_info_url, wait_until="networkidle", timeout=20000)
            await self.page.wait_for_selector(DEVICE_TABLE_CONTAINER, timeout=15000)
        except PlaywrightTimeoutError as e:
            if "login" in self.page.url.lower():
                raise AuthenticationRequiredError(
                    "Redirected to login while navigating to Device Information"
                ) from e
            raise ExtractionError(
                f"Could not open Device Information page: {e}. "
                "Page structure may have changed — check selectors.py."
            ) from e
        except PlaywrightError as e:
            raise TargetUnavailableError(f"Navigation failure opening Device Information: {e}") from e

    async def get_equipment_data(self) -> list[dict]:
        """Returns raw-but-canonically-keyed equipment rows (see
        _blank_canonical_row for the shape). Normalization into
        EquipmentRecord/validation happens in EquipmentExtractor, not here.

        Tries the target's own JSON list API first (requirement #15);
        falls back to DOM scraping of the same table if that fails
        structurally, per requirement #15's "must remain capable of
        falling back to browser/DOM extraction if necessary."
        """
        try:
            return await self._get_equipment_data_api()
        except (AuthenticationRequiredError, TargetUnavailableError):
            raise
        except ExtractionError as e:
            logger.warning("API extraction failed (%s) — falling back to DOM scraping", e)
            return await self._get_equipment_data_dom()

    async def _get_equipment_data_api(self) -> list[dict]:
        limit = DEVICE_LIST_API_PAGE_SIZE
        offset = 0
        all_rows: list[dict] = []
        page_num = 1

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
                resp = await self.page.request.get(
                    self._device_list_api_url,
                    params=params,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    timeout=15000,
                )
            except PlaywrightError as e:
                raise TargetUnavailableError(f"Could not reach device list API: {e}") from e

            if resp.status != 200:
                raise ExtractionError(f"Device list API returned HTTP {resp.status}")

            try:
                data = await resp.json()
            except Exception as e:
                raise ExtractionError(f"Device list API did not return valid JSON: {e}") from e

            if not isinstance(data, dict) or "rows" not in data:
                raise ExtractionError(
                    f"Device list API response missing 'rows' — schema may have changed. "
                    f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )

            rows = data["rows"]
            all_rows.extend(_map_api_row_to_canonical(r) for r in rows)

            total = data.get("total", len(all_rows))
            offset += limit
            if not rows or offset >= total:
                break
            page_num += 1
            if page_num > _MAX_PAGINATION_PAGES:
                logger.warning("Hit API pagination sanity guard (%s pages) — stopping", _MAX_PAGINATION_PAGES)
                break

        return all_rows

    async def _get_equipment_data_dom(self) -> list[dict]:
        try:
            headers = await self._read_headers()
            if not headers:
                raise ExtractionError(
                    "Device Information table has no readable header row — "
                    "page structure may have changed."
                )

            all_rows: list[dict] = []
            page_num = 1
            while True:
                rows = await self._read_body_rows(headers)
                all_rows.extend(_map_dom_row_to_canonical(r) for r in rows)
                if not await self._go_to_next_page():
                    break
                page_num += 1
                if page_num > _MAX_PAGINATION_PAGES:
                    logger.warning("Hit pagination sanity guard (%s pages) — stopping", _MAX_PAGINATION_PAGES)
                    break

            return all_rows
        except (ExtractionError, AuthenticationRequiredError, TargetUnavailableError):
            raise
        except PlaywrightError as e:
            raise ExtractionError(f"Failed reading Device Information table: {e}") from e

    async def _read_headers(self) -> list[str]:
        header_cells = self.page.locator(f"{DEVICE_TABLE_HEADER_ROW} {DEVICE_TABLE_HEADER_CELL}")
        count = await header_cells.count()
        return [(await header_cells.nth(i).inner_text()).strip() for i in range(count)]

    async def _read_body_rows(self, headers: list[str]) -> list[dict[str, str]]:
        rows_locator = self.page.locator(DEVICE_TABLE_BODY_ROW)
        row_count = await rows_locator.count()
        rows: list[dict[str, str]] = []
        for r in range(row_count):
            cells = rows_locator.nth(r).locator(DEVICE_TABLE_BODY_CELL)
            cell_count = await cells.count()
            row = {
                headers[c]: (await cells.nth(c).inner_text()).strip()
                for c in range(min(cell_count, len(headers)))
            }
            if row:
                rows.append(row)
        return rows

    async def _go_to_next_page(self) -> bool:
        next_btn = self.page.locator(PAGINATION_NEXT_BUTTON).first
        try:
            if await next_btn.count() == 0:
                return False
            if not await next_btn.is_enabled():
                return False
            await next_btn.click(timeout=5000)
            await self.page.wait_for_timeout(500)  # let the table re-render
            return True
        except PlaywrightError:
            return False

    async def close(self) -> None:
        """No-op: the browser/page/context lifecycle is owned by
        BrowserManager and stays alive across polls (requirement #12)."""
        return None
