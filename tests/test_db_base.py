"""
Unit tests for db/base.py's init_engine() -- specifically the SQLite WAL
+ busy_timeout fix (see its docstring / CHANGELOG.md's 3.0.0 entry for
why: monitoring.combined_worker crashed twice with "database is locked"
from concurrent access between the worker and web dashboard processes,
both hitting the same SQLite file).
"""
from __future__ import annotations

from dataclasses import replace

from sqlalchemy import text

import db.base as db_base
from monitoring.config import load_settings


def _sqlite_settings(tmp_path):
    return replace(load_settings(), database_url_override=f"sqlite:///{tmp_path / 'test.db'}")


def test_sqlite_engine_enables_wal_mode(tmp_path):
    db_base.init_engine(_sqlite_settings(tmp_path))
    with db_base.get_session() as session:
        mode = session.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_sqlite_engine_sets_busy_timeout(tmp_path):
    db_base.init_engine(_sqlite_settings(tmp_path))
    with db_base.get_session() as session:
        timeout_ms = session.execute(text("PRAGMA busy_timeout")).scalar()
    assert timeout_ms == 30000


def test_postgres_url_does_not_attempt_sqlite_setup():
    settings = replace(load_settings(), database_url_override="postgresql+psycopg2://u:p@localhost:5432/db")
    # create_engine() is lazy -- this must not raise or try to actually
    # connect, and must not register the SQLite-only PRAGMA listener.
    engine = db_base.init_engine(settings)
    assert engine.dialect.name == "postgresql"
    assert str(engine.url).startswith("postgresql+psycopg2")


def test_concurrent_reader_and_writer_do_not_raise_database_is_locked(tmp_path):
    """Regression test for the actual bug: a writer holding a transaction
    open must not make a concurrent reader (or a second writer, given
    busy_timeout) fail immediately -- this is exactly the
    monitoring.combined_worker (writer) vs. dashboard (reader) crash from
    CHANGELOG.md's 3.0.0 entry, reproduced directly against two separate
    connections to the same file."""
    from db.models import AlertRecipient

    settings = _sqlite_settings(tmp_path)
    db_base.init_engine(settings)
    db_base.create_all()

    writer = db_base._SessionLocal()
    writer.add(AlertRecipient(email="writer@example.com", active=True))
    writer.flush()  # starts a write transaction, held open -- not committed yet

    reader = db_base._SessionLocal()
    try:
        # Under the old rollback-journal default, this would raise
        # "database is locked" immediately instead of just seeing the
        # pre-write state. WAL mode lets it succeed.
        count = reader.execute(text("SELECT COUNT(*) FROM alert_recipient")).scalar()
        assert count == 0  # writer hasn't committed yet -- correct isolation, no crash
    finally:
        reader.close()
        writer.commit()
        writer.close()
