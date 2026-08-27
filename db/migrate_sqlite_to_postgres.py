"""
One-time migration: copies every row from a SQLite demo database into
the Postgres database configured in `.env` (DB_HOST/DB_NAME/DB_USER/
DB_PASSWORD, or DATABASE_URL if set), preserving primary keys, then
fixes Postgres's auto-increment sequences to continue past the migrated
max id per table -- otherwise the next INSERT from the running app
would collide with a migrated id.

Why this exists: this project's local dev/demo environment moved from
SQLite to Postgres on 2026-08-27 to fix a recurring "database is locked"
crash (SQLite's single-writer model colliding under the app's own
concurrent equipment-monitoring + orders loops -- see CHANGELOG.md).
This script is what carried the existing demo data across rather than
starting over on a fresh, empty Postgres database.

Tables are copied in Base.metadata.sorted_tables order, which SQLAlchemy
topologically sorts by foreign-key dependency -- so a referenced table
(e.g. equipment) is always populated before whatever references it
(e.g. equipment_current_state), avoiding FK violations.

SAFETY: refuses to run if the target Postgres database already has ANY
data in ANY of these tables, unless --force is passed -- re-running this
against an already-migrated (or otherwise populated) Postgres database
would insert duplicate rows with colliding primary keys, not update or
skip them. This is a one-time carry-over tool, not a sync/reconcile tool.

Run:
    python -m db.migrate_sqlite_to_postgres
    python -m db.migrate_sqlite_to_postgres --sqlite-path data/demo.db --force
"""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, func, select, text

from db.models import Base
from monitoring.config import load_settings


def _target_has_any_data(pg_conn) -> bool:
    for table in Base.metadata.sorted_tables:
        count = pg_conn.execute(select(func.count()).select_from(table)).scalar()
        if count:
            return True
    return False


def migrate(sqlite_path: str, force: bool = False) -> int:
    settings = load_settings()

    sqlite_engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    pg_engine = create_engine(settings.database_url, future=True)

    sqlite_conn = sqlite_engine.connect()
    pg_conn = pg_engine.connect()

    try:
        if not force and _target_has_any_data(pg_conn):
            raise SystemExit(
                "Target Postgres database already has data in at least one table. "
                "Refusing to migrate (would create duplicate/colliding rows). "
                "Pass --force only if you're certain this is safe (e.g. a truly empty schema check failed)."
            )

        total_rows = 0
        for table in Base.metadata.sorted_tables:
            rows = sqlite_conn.execute(table.select()).mappings().all()
            if not rows:
                print(f"{table.name}: 0 rows (skipped)")
                continue

            pg_conn.execute(table.insert(), [dict(r) for r in rows])
            pg_conn.commit()
            print(f"{table.name}: {len(rows)} rows migrated")
            total_rows += len(rows)

            # Reset the sequence backing this table's serial/identity PK
            # (if any) to continue past the migrated max id. Postgres-only
            # (pg_get_serial_sequence doesn't exist elsewhere) -- SQLite's
            # own rowid-based autoincrement needs no equivalent step, and
            # this also makes the function testable against a SQLite
            # stand-in target (see tests/test_migrate_sqlite_to_postgres.py).
            pk_cols = [c.name for c in table.primary_key.columns]
            if pg_conn.engine.dialect.name == "postgresql" and len(pk_cols) == 1:
                pk = pk_cols[0]
                seq_name = pg_conn.execute(
                    text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                    {"tbl": table.name, "col": pk},
                ).scalar()
                if seq_name:
                    pg_conn.execute(
                        text(
                            f'SELECT setval(:seq, COALESCE((SELECT MAX({pk}) FROM {table.name}), 1), '
                            f'(SELECT MAX({pk}) FROM {table.name}) IS NOT NULL)'
                        ),
                        {"seq": seq_name},
                    )
                    pg_conn.commit()

        print(f"\nTotal rows migrated: {total_rows}")
        return total_rows
    finally:
        sqlite_conn.close()
        pg_conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate data/demo.db (SQLite) into the Postgres database configured in .env")
    parser.add_argument("--sqlite-path", default="data/demo.db", help="Path to the source SQLite file (default: data/demo.db)")
    parser.add_argument("--force", action="store_true", help="Migrate even if the target Postgres database already has data (NOT recommended -- see module docstring)")
    args = parser.parse_args()

    migrate(args.sqlite_path, force=args.force)


if __name__ == "__main__":
    main()
