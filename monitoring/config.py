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
    auth_manual_timeout_seconds: int
    auto_solve_captcha: bool

    poll_interval_seconds: int
    poc_iterations: int

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    database_url_override: str

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    smtp_use_tls: bool
    alert_on_escalation: bool
    send_recovery_notifications: bool

    web_secret_key: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.target_username and self.target_password)

    @property
    def database_url(self) -> str:
        # DATABASE_URL, if set, overrides the DB_* fields entirely — e.g.
        # for pointing at SQLite for a quick local demo/CI run without
        # Docker/Postgres, or a managed Postgres URL in some deployments.
        # docker-compose.yml/db/init_db.py still default to the DB_* path.
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)


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
        auth_manual_timeout_seconds=_get_int("AUTH_MANUAL_TIMEOUT_SECONDS", 300),
        auto_solve_captcha=_get_bool("AUTO_SOLVE_CAPTCHA", False),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60),
        poc_iterations=_get_int("POC_ITERATIONS", 3),
        db_host=os.getenv("DB_HOST", "localhost").strip(),
        db_port=_get_int("DB_PORT", 5432),
        db_name=os.getenv("DB_NAME", "equipment_monitor").strip(),
        db_user=os.getenv("DB_USER", "equipment_monitor").strip(),
        db_password=os.getenv("DB_PASSWORD", "").strip(),
        database_url_override=os.getenv("DATABASE_URL", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=_get_int("SMTP_PORT", 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL", "").strip(),
        smtp_use_tls=_get_bool("SMTP_USE_TLS", True),
        alert_on_escalation=_get_bool("ALERT_ON_ESCALATION", True),
        send_recovery_notifications=_get_bool("SEND_RECOVERY_NOTIFICATIONS", True),
        web_secret_key=os.getenv("WEB_SECRET_KEY", "").strip(),
    )


PROJECT_ROOT = _PROJECT_ROOT
