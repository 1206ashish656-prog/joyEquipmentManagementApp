"""
Configuration loading for the monitoring engine.

All secrets/config come from environment variables (loaded from .env via
python-dotenv). Nothing here should ever be hardcoded — see project
requirement: "The target application's credentials must never be
hardcoded."
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    target_base_url: str
    target_login_url: str
    target_username: str
    target_password: str

    storage_state_path: Path
    headless: bool

    poll_interval_seconds: int
    poc_iterations: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.target_username and self.target_password)


def load_settings() -> Settings:
    storage_state_path = _PROJECT_ROOT / os.getenv(
        "STORAGE_STATE_PATH", "data/storage_state/session.json"
    )
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        target_base_url=os.getenv("TARGET_BASE_URL", "").strip(),
        target_login_url=os.getenv("TARGET_LOGIN_URL", "").strip(),
        target_username=os.getenv("TARGET_USERNAME", "").strip(),
        target_password=os.getenv("TARGET_PASSWORD", "").strip(),
        storage_state_path=storage_state_path,
        headless=_get_bool("HEADLESS", False),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
        poc_iterations=_get_int("POC_ITERATIONS", 3),
    )


PROJECT_ROOT = _PROJECT_ROOT
