"""
Engine/session setup. Sync SQLAlchemy on purpose: the monitoring worker is
async (Playwright), but DB writes here are small and infrequent (once per
poll cycle) — running them via asyncio.to_thread() from the async worker
(see monitoring/worker.py) is simpler and less error-prone than maintaining
a second async ORM stack for this scale (section 29: don't overengineer
for 1-100 users / one monitoring session).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from monitoring.config import Settings, load_settings

from .models import Base

_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(settings: Settings | None = None, echo: bool = False):
    """Creates the module-level engine/session factory. Safe to call more
    than once (e.g. once in the worker, once in tests) — later calls
    replace the engine.

    For SQLite specifically: enables WAL (Write-Ahead Logging) mode plus a
    busy_timeout on every new connection. Without this, the default
    rollback-journal mode takes an exclusive lock for the whole duration
    of any write, so a concurrent reader/writer (e.g. the dashboard
    process and the background worker process both hitting the same
    demo.db file) intermittently fails with "database is locked" instead
    of waiting — this crashed monitoring.combined_worker twice in one
    session (see CHANGELOG.md's 3.0.0 entry) before this fix. WAL lets
    readers and a single writer coexist without blocking each other;
    busy_timeout is the safety net for the rarer writer-vs-writer case
    (e.g. two worker cycles overlapping), making SQLite wait and retry for
    up to 30s instead of failing immediately. No effect on Postgres.
    """
    global _engine, _SessionLocal
    settings = settings or load_settings()
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    _engine = create_engine(settings.database_url, echo=echo, future=True, connect_args=connect_args)

    if is_sqlite:
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def create_all() -> None:
    """Creates all tables that don't exist yet. Used instead of Alembic
    for now — see docs/target_application_integration_spec.md's sibling
    note in README about deferring migrations to Phase 6 hardening."""
    if _engine is None:
        init_engine()
    Base.metadata.create_all(_engine)


@contextmanager
def get_session() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
