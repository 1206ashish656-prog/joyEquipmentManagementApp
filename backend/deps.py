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
