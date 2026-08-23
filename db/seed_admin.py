"""
Bootstraps the first admin user for the dashboard's own login (distinct
from the target application's credentials). Needed because /users itself
requires an admin to already be logged in — the classic first-admin
chicken-and-egg problem.

Run:
    python -m db.seed_admin --name "Ashish" --email admin@example.com --password "..."

If a user with that email already exists, updates their password/role
instead of failing (idempotent — safe to re-run to reset a forgotten
password).
"""
from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from backend.security import hash_password
from db import base as db_base
from db.models import User


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update the first admin user")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Omit to be prompted (not echoed to the terminal)")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    email = args.email.strip().lower()

    db_base.init_engine()
    db_base.create_all()

    with db_base.get_session() as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(name=args.name, email=email, role="admin", active=True)
            session.add(user)
            action = "Created"
        else:
            user.name = args.name
            user.role = "admin"
            user.active = True
            action = "Updated"
        user.password_hash = hash_password(password)

    print(f"{action} admin user {email}.")


if __name__ == "__main__":
    main()
