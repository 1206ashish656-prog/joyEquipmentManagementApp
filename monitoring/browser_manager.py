"""
BrowserManager: owns the single persistent Playwright browser/context/page
for the monitoring worker's lifetime.

Project requirement #12: "Do NOT implement: every minute launch browser,
login, scrape, close browser." This class is explicitly the thing that
prevents that — start() is called once when the worker boots, and the same
context/page is reused across every poll. Session persistence across
*process restarts* is handled by saving/loading Playwright's storage_state
(cookies + localStorage) to disk, per requirement #12's "retain relevant
authentication state such as cookies/local storage".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, storage_state_path: Path, headless: bool = False):
        self._storage_state_path = storage_state_path
        self._headless = headless

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserManager.start() must be called first")
        return self._page

    async def start(self) -> Page:
        """Launch the browser+context ONCE. Reuses an existing storage
        state file (prior authenticated session) if present, so a restart
        of the worker doesn't necessarily require a fresh login."""
        if self._page is not None:
            return self._page

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)

        has_saved_session = self._storage_state_path.exists()
        context_kwargs = {}
        if has_saved_session:
            logger.info("Reusing saved session from %s", self._storage_state_path)
            context_kwargs["storage_state"] = str(self._storage_state_path)
        else:
            logger.info("No saved session found at %s — starting fresh", self._storage_state_path)

        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        return self._page

    async def persist_session(self) -> None:
        """Call after a successful authenticate() (or periodically) to
        write the current cookies/localStorage to disk so a future process
        start can reuse this session without logging in again."""
        if self._context is None:
            raise RuntimeError("BrowserManager.start() must be called first")
        self._storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self._storage_state_path))
        logger.info("Session state persisted to %s", self._storage_state_path)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
