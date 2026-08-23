"""
Shared FastAPI dependencies: DB session, current user, RBAC.

Unauthenticated access is handled by raising NotAuthenticated and letting
main.py's exception handler redirect to /login — keeps every route handler
free of manual "if not user: return RedirectResponse(...)" boilerplate.
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.security import verify_session
from db import base as db_base
from db.models import User
from monitoring.config import Settings, load_settings

SESSION_COOKIE_NAME = "session"


class NotAuthenticated(Exception):
    pass


def get_settings() -> Settings:
    return load_settings()


def get_db() -> Iterator[Session]:
    with db_base.get_session() as session:
        yield session


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = verify_session(token, settings.web_secret_key)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.active:
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_operations(user: User = Depends(require_user)) -> User:
    """Equipment monitoring area (dashboard, active faults, alert
    subscriptions): admins and ground/operations staff. Venue partners are
    scoped to Order Summary only (require_venue_partner) and have no
    business need to see equipment health."""
    if user.role not in ("admin", "operations"):
        raise HTTPException(status_code=403, detail="Operations or admin access required")
    return user


def require_venue_partner(user: User = Depends(require_user)) -> User:
    """Order Summary: admins (unrestricted) and venue partners (scoped to
    their own venue's machine(s) inside the route handler itself — see
    backend/api/orders.py). Operations staff have no order-data access."""
    if user.role not in ("admin", "venue_partner"):
        raise HTTPException(status_code=403, detail="Venue partner or admin access required")
    return user


def home_url_for(user: User | None) -> str:
    """Where a logged-in user should land — the first page their role is
    actually allowed to see. Used for the post-login redirect and the nav
    brand link, so a venue_partner (who can't see "/") never gets bounced
    into a 403 immediately after logging in."""
    if user is not None and user.role == "venue_partner":
        return "/orders/summary"
    return "/"
