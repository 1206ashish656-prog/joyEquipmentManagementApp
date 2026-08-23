"""
TargetApplicationClient: the ONLY module that talks to jwintell.com.

Everything else in the pipeline works with normalized data (see
equipment_extractor.py). If the target site's markup changes, this file
(plus selectors.py) is what should need updating — see requirement #34.

Selectors are pending Phase 1 discovery confirmation (see selectors.py).
Run discovery/inspect.py first and update selectors.py from its output
before relying on this against the real site.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import Settings
from .models import AuthenticationRequiredError, ExtractionError, TargetUnavailableError
from .selectors import (
    AUTHENTICATED_MARKER_TEXT,
    DEVICE_TABLE_BODY_CELL,
    DEVICE_TABLE_BODY_ROW,
    DEVICE_TABLE_CONTAINER,
    DEVICE_TABLE_HEADER_CELL,
    DEVICE_TABLE_HEADER_ROW,
    LOGIN_PASSWORD_INPUT,
    LOGIN_USERNAME_INPUT,
    NAV_DEVICE_INFORMATION,
    NAV_EQUIPMENT_MANAGEMENT,
    PAGINATION_NEXT_BUTTON,
)

logger = logging.getLogger(__name__)

_MAX_PAGINATION_PAGES = 200  # sanity guard, not an expected real value


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


class TargetApplicationClient:
    def __init__(self, page: Page, settings: Settings):
        self.page = page
        self.settings = settings
        self._dashboard_url = _derive_dashboard_url(settings)

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
        print("=" * 70)
        input("Press Enter here once you are logged in and see the console/dashboard... ")

        if not await self.is_session_valid():
            raise AuthenticationRequiredError(
                "Still redirected to login after manual login attempt. Check "
                "credentials, or that the CAPTCHA/login actually completed."
            )
        logger.info("Authentication confirmed — session established.")

    async def open_device_information(self) -> None:
        try:
            await self.page.click(NAV_EQUIPMENT_MANAGEMENT, timeout=10000)
            await self.page.click(NAV_DEVICE_INFORMATION, timeout=10000)
            await self.page.wait_for_selector(DEVICE_TABLE_CONTAINER, timeout=15000)
        except PlaywrightTimeoutError as e:
            if "login" in self.page.url.lower():
                raise AuthenticationRequiredError(
                    "Redirected to login while navigating to Device Information"
                ) from e
            raise ExtractionError(
                f"Could not open Equipment Management -> Device Information: {e}. "
                "Page structure may have changed — check selectors.py."
            ) from e
        except PlaywrightError as e:
            raise TargetUnavailableError(f"Navigation failure opening Device Information: {e}") from e

    async def get_equipment_data(self) -> list[dict[str, str]]:
        """Reads the Device Information table across all pages. Returns raw
        rows keyed by the table's own header text — normalization into
        EquipmentRecord happens in EquipmentExtractor, not here."""
        try:
            headers = await self._read_headers()
            if not headers:
                raise ExtractionError(
                    "Device Information table has no readable header row — "
                    "page structure may have changed."
                )

            all_rows: list[dict[str, str]] = []
            page_num = 1
            while True:
                all_rows.extend(await self._read_body_rows(headers))
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
