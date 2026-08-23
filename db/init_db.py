"""
Creates all tables in the configured Postgres database (idempotent — safe
to re-run). Run once after `docker compose up -d`:

    python -m db.init_db
"""
from __future__ import annotations

from . import base


def main() -> None:
    base.init_engine()
    base.create_all()
    print("Database tables created (or already existed).")


if __name__ == "__main__":
    main()
