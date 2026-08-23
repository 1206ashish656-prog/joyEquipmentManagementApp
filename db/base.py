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

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from monitoring.config import Settings, load_settings

from .models import Base

_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(settings: Settings | None = None, echo: bool = False):
    """Creates the module-level engine/session factory. Safe to call more
    than once (e.g. once in the worker, once in tests) — later calls
    replace the engine."""
    global _engine, _SessionLocal
    settings = settings or load_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    _engine = create_engine(settings.database_url, echo=echo, future=True, connect_args=connect_args)
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
