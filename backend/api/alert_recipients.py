"""
Admin-only management of AlertRecipient rows — plain email addresses
that get every Critical-severity (malfunction/offline) alert without
needing a dashboard account, per explicit request: "a simpler flat list
of recipient emails (not tied to dashboard logins)". Separate from
AlertSubscription (which requires a real User) — see
services/alert_engine.py's get_recipients() for how both feed in.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.templating import templates
from db.models import AlertRecipient, User

router = APIRouter()


@router.get("/alert-recipients", response_class=HTMLResponse)
def list_alert_recipients(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    recipients = db.execute(select(AlertRecipient).order_by(AlertRecipient.email)).scalars().all()
    return templates.TemplateResponse(
        request, "alert_recipients.html", {"user": admin, "recipients": recipients, "error": None}
    )


@router.post("/alert-recipients")
def create_alert_recipient(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()

    def _rerender(error: str):
        recipients = db.execute(select(AlertRecipient).order_by(AlertRecipient.email)).scalars().all()
        return templates.TemplateResponse(
            request, "alert_recipients.html", {"user": admin, "recipients": recipients, "error": error},
            status_code=400,
        )

    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return _rerender(f"'{email}' doesn't look like a valid email address.")

    existing = db.execute(select(AlertRecipient).where(AlertRecipient.email == email)).scalar_one_or_none()
    if existing is not None:
        return _rerender(f"{email} is already on the alert recipient list.")

    db.add(AlertRecipient(email=email, name=name.strip() or None, active=True))
    return RedirectResponse(url="/alert-recipients", status_code=303)


@router.post("/alert-recipients/{recipient_id}/deactivate")
def deactivate_alert_recipient(recipient_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(AlertRecipient, recipient_id)
    if target is not None:
        target.active = False
    return RedirectResponse(url="/alert-recipients", status_code=303)


@router.post("/alert-recipients/{recipient_id}/activate")
def activate_alert_recipient(recipient_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(AlertRecipient, recipient_id)
    if target is not None:
        target.active = True
    return RedirectResponse(url="/alert-recipients", status_code=303)
