"""Tests for db/migrate_sqlite_to_postgres.py's core logic. Uses a
second SQLite file standing in for "Postgres" (the function is generic
over any SQLAlchemy engine URL, and this project's tests deliberately
don't require a real Postgres instance to run -- same philosophy as
every other test file here)."""
from __future__ import annotations

from dataclasses import replace

from sqlalchemy import create_engine, select

from db.migrate_sqlite_to_postgres import migrate
from db.models import AlertRecipient, Base, InventoryItem, User
from monitoring.config import load_settings


def _make_sqlite_db(path, seed=True):
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    if seed:
        with engine.begin() as conn:
            conn.execute(
                InventoryItem.__table__.insert(),
                [{"key": "oranges", "name": "Oranges", "unit": "boxes", "pieces_per_carton": None, "current_stock": 42}],
            )
            conn.execute(
                User.__table__.insert(),
                [{"name": "Admin", "email": "admin@example.com", "role": "admin", "active": True, "password_hash": "x"}],
            )
    engine.dispose()


def _make_sqlite_db_with_unrelated_data(path):
    """Seeds a table the migration tests below never touch, so the
    target "already has data" (triggering the guard) without its rows
    colliding on primary key with anything the source will insert."""
    engine = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(AlertRecipient.__table__.insert(), [{"email": "preexisting@example.com", "active": True}])
    engine.dispose()


def test_migrate_copies_all_rows(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _make_sqlite_db(source, seed=True)
    _make_sqlite_db(target, seed=False)

    monkeypatch.setattr(
        "db.migrate_sqlite_to_postgres.load_settings",
        lambda: replace(load_settings(), database_url_override=f"sqlite:///{target}"),
    )

    total = migrate(str(source))
    assert total == 2

    target_engine = create_engine(f"sqlite:///{target}", future=True)
    with target_engine.connect() as conn:
        items = conn.execute(select(InventoryItem)).all()
        assert len(items) == 1
        users = conn.execute(select(User)).all()
        assert len(users) == 1


def test_migrate_refuses_when_target_already_has_data(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _make_sqlite_db(source, seed=True)
    _make_sqlite_db(target, seed=True)  # target already populated

    monkeypatch.setattr(
        "db.migrate_sqlite_to_postgres.load_settings",
        lambda: replace(load_settings(), database_url_override=f"sqlite:///{target}"),
    )

    import pytest
    with pytest.raises(SystemExit):
        migrate(str(source))


def test_migrate_force_overrides_the_guard(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _make_sqlite_db(source, seed=True)
    _make_sqlite_db_with_unrelated_data(target)  # target has data, but nothing that collides on id

    monkeypatch.setattr(
        "db.migrate_sqlite_to_postgres.load_settings",
        lambda: replace(load_settings(), database_url_override=f"sqlite:///{target}"),
    )

    total = migrate(str(source), force=True)
    assert total == 2  # migrated anyway, alongside the pre-existing unrelated row


def test_migrate_empty_source_copies_nothing(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _make_sqlite_db(source, seed=False)
    _make_sqlite_db(target, seed=False)

    monkeypatch.setattr(
        "db.migrate_sqlite_to_postgres.load_settings",
        lambda: replace(load_settings(), database_url_override=f"sqlite:///{target}"),
    )

    total = migrate(str(source))
    assert total == 0
