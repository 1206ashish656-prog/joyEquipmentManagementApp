"""
SessionManager: the session-awareness rule from requirement #2/#36 lives
here — "only authenticate when the previous session has ended, expired,
become invalid, or the target redirects to login." Nothing else in the
codebase should call TargetApplicationClient.authenticate() directly.
"""
from __future__ import annotations

import logging

from .browser_manager import BrowserManager
from .models import MonitoringState
from .target_client import TargetApplicationClient

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, browser_manager: BrowserManager, client: TargetApplicationClient):
        self._browser_manager = browser_manager
        self._client = client
        self.state: MonitoringState = MonitoringState.OK

    async def ensure_authenticated(self) -> None:
        """Reuses the existing session if valid. Only authenticates
        (triggering the human CAPTCHA workflow) when it is not."""
        if await self._client.is_session_valid():
            logger.info("Existing session is valid — reusing it (no login performed).")
            self.state = MonitoringState.OK
            return

        logger.info("Session invalid/expired — authentication required.")
        self.state = MonitoringState.AUTHENTICATION_REQUIRED
        await self._client.authenticate()
        await self._browser_manager.persist_session()
        self.state = MonitoringState.OK
