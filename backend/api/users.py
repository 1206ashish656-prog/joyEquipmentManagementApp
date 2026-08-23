"""Admin-only user management (spec section 19's "admin can manage
users")."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.security import hash_password
from backend.templating import templates
from db.models import User, UserRole, VenueMapping

router = APIRouter()

_VALID_ROLES = {r.value for r in UserRole}


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.name)).scalars().all()
    venues = sorted({v for (v,) in db.execute(select(VenueMapping.venue_provider).distinct())})
    return templates.TemplateResponse(
        request, "users.html", {"user": admin, "users": users, "venues": venues, "error": None}
    )


@router.post("/users")
def create_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(UserRole.OPERATIONS.value),
    venue_provider: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    role = role if role in _VALID_ROLES else UserRole.OPERATIONS.value
    venue_provider = venue_provider.strip() or None

    def _rerender(error: str):
        users = db.execute(select(User).order_by(User.name)).scalars().all()
        venues = sorted({v for (v,) in db.execute(select(VenueMapping.venue_provider).distinct())})
        return templates.TemplateResponse(
            request,
            "users.html",
            {"user": admin, "users": users, "venues": venues, "error": error},
            status_code=400,
        )

    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return _rerender(f"A user with email {email} already exists.")

    if role == UserRole.VENUE_PARTNER.value and not venue_provider:
        return _rerender("A venue partner user must be assigned a venue.")

    db.add(
        User(
            name=name.strip(),
            email=email,
            role=role,
            # Only venue_partner accounts carry a venue — keeps the field
            # meaningless-but-set-by-accident from ever happening for the
            # other two roles.
            venue_provider=venue_provider if role == UserRole.VENUE_PARTNER.value else None,
            active=True,
            password_hash=hash_password(password),
        )
    )
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target is not None:
        target.active = False
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/activate")
def activate_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target is not None:
        target.active = True
    return RedirectResponse(url="/users", status_code=303)
