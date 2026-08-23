from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import SESSION_COOKIE_NAME, get_current_user, get_db, get_settings
from backend.security import sign_session, verify_password
from backend.templating import templates
from db.models import User
from monitoring.config import Settings

router = APIRouter()


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()

    # Constant-shape check: still run verify_password against a dummy hash
    # when the user doesn't exist, so a login attempt for an unknown email
    # doesn't respond measurably faster than one for a known email.
    if user is None or not user.active:
        verify_password(password, None)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )

    if not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password"}, status_code=401
        )

    token = sign_session(user.id, settings.web_secret_key)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, httponly=True, samesite="lax", max_age=7 * 24 * 3600,
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
