"""Active faults (spec section 23) + per-user alert subscriptions (spec
section 19/9's "notification recipients")."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_operations
from backend.templating import templates
from db.models import AlertSubscription, Equipment, FaultIncident, IncidentStatus, User

router = APIRouter()


@router.get("/faults", response_class=HTMLResponse)
def active_faults(request: Request, user: User = Depends(require_operations), db: Session = Depends(get_db)):
    rows = db.execute(
        select(FaultIncident, Equipment)
        .join(Equipment, FaultIncident.equipment_id == Equipment.id)
        .where(FaultIncident.status == IncidentStatus.ACTIVE)
        .order_by(FaultIncident.started_at.desc())
    ).all()

    now = datetime.now(timezone.utc)
    incidents = []
    for incident, equipment in rows:
        started = incident.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        incidents.append({"incident": incident, "equipment": equipment, "duration": now - started})

    return templates.TemplateResponse(request, "faults.html", {"user": user, "incidents": incidents})


@router.get("/faults/{incident_id}", response_class=HTMLResponse)
def fault_detail(incident_id: int, request: Request, user: User = Depends(require_operations), db: Session = Depends(get_db)):
    incident = db.get(FaultIncident, incident_id)
    if incident is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": user}, status_code=404)
    equipment = db.get(Equipment, incident.equipment_id)

    started = incident.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if incident.status == IncidentStatus.ACTIVE:
        end = datetime.now(timezone.utc)
    else:
        end = incident.resolved_at
        if end and end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    duration = (end - started) if end else None

    return templates.TemplateResponse(
        request, "fault_detail.html", {"user": user, "incident": incident, "equipment": equipment, "duration": duration}
    )


@router.get("/subscriptions", response_class=HTMLResponse)
def my_subscriptions(request: Request, user: User = Depends(require_operations), db: Session = Depends(get_db)):
    subs = db.execute(
        select(AlertSubscription, Equipment)
        .outerjoin(Equipment, AlertSubscription.equipment_id == Equipment.id)
        .where(AlertSubscription.user_id == user.id)
        .order_by(AlertSubscription.id)
    ).all()
    equipment_list = db.execute(select(Equipment).where(Equipment.active.is_(True)).order_by(Equipment.name)).scalars().all()

    return templates.TemplateResponse(
        request,
        "subscriptions.html",
        {"user": user, "subscriptions": subs, "equipment_list": equipment_list},
    )


@router.post("/subscriptions")
def create_subscription(
    request: Request,
    equipment_id: str = Form(""),
    severity: str = Form(""),
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
):
    db.add(
        AlertSubscription(
            user_id=user.id,
            equipment_id=int(equipment_id) if equipment_id else None,
            severity=severity or None,
            notification_type="email",
            enabled=True,
        )
    )
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/{subscription_id}/toggle")
def toggle_subscription(subscription_id: int, user: User = Depends(require_operations), db: Session = Depends(get_db)):
    sub = db.get(AlertSubscription, subscription_id)
    if sub is not None and (sub.user_id == user.id or user.role == "admin"):
        sub.enabled = not sub.enabled
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/{subscription_id}/delete")
def delete_subscription(subscription_id: int, user: User = Depends(require_operations), db: Session = Depends(get_db)):
    sub = db.get(AlertSubscription, subscription_id)
    if sub is not None and (sub.user_id == user.id or user.role == "admin"):
        db.delete(sub)
    return RedirectResponse(url="/subscriptions", status_code=303)
