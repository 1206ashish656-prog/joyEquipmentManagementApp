"""Admin-only user management (spec section 19's "admin can manage
users").

Editing/creating a user's ROLE has one extra gate on top of the usual
require_admin check: only a super admin (User.is_super_admin, see
db/models.py) may grant or revoke the "admin" role itself. A regular
admin can still freely edit everything else on any user (name, email,
venue, active, password) and can freely switch a non-admin between
operations/venue_partner -- they just can't touch the admin role
either direction. Enforced in BOTH create_user and update_user so a
regular admin can't route around an edit-only gate by just creating a
new admin instead."""
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


def _venues(db: Session) -> list[str]:
    return sorted({v for (v,) in db.execute(select(VenueMapping.venue_provider).distinct())})


@router.get("/users", response_class=HTMLResponse)
def list_users(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.execute(select(User).order_by(User.name)).scalars().all()
    return templates.TemplateResponse(
        request, "users.html", {"user": admin, "users": users, "venues": _venues(db), "error": None}
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
        return templates.TemplateResponse(
            request,
            "users.html",
            {"user": admin, "users": db.execute(select(User).order_by(User.name)).scalars().all(), "venues": _venues(db), "error": error},
            status_code=400,
        )

    if role == UserRole.ADMIN.value and not admin.is_super_admin:
        return _rerender("Only a super admin can create an admin user.")

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


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(user_id: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse(url="/users", status_code=303)
    return templates.TemplateResponse(
        request, "user_edit.html", {"user": admin, "target": target, "venues": _venues(db), "error": None}
    )


@router.post("/users/{user_id}/edit")
def update_user(
    user_id: int,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    venue_provider: str = Form(""),
    password: str = Form(""),
    active: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return RedirectResponse(url="/users", status_code=303)

    email = email.strip().lower()
    role = role if role in _VALID_ROLES else target.role
    venue_provider = venue_provider.strip() or None

    def _rerender(error: str):
        return templates.TemplateResponse(
            request,
            "user_edit.html",
            {"user": admin, "target": target, "venues": _venues(db), "error": error},
            status_code=400,
        )

    # Only a super admin may grant or revoke the admin role itself --
    # every other field/role-pair remains freely editable by any admin.
    attempting_admin_change = role != target.role and (role == UserRole.ADMIN.value or target.role == UserRole.ADMIN.value)
    if attempting_admin_change and not admin.is_super_admin:
        return _rerender("Only a super admin can grant or revoke admin access.")

    existing = db.execute(select(User).where(User.email == email, User.id != user_id)).scalar_one_or_none()
    if existing is not None:
        return _rerender(f"A user with email {email} already exists.")

    if role == UserRole.VENUE_PARTNER.value and not venue_provider:
        return _rerender("A venue partner user must be assigned a venue.")

    target.name = name.strip()
    target.email = email
    target.role = role
    target.venue_provider = venue_provider if role == UserRole.VENUE_PARTNER.value else None
    target.active = active == "on"
    if password.strip():  # leave blank to keep the current password
        target.password_hash = hash_password(password)

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
