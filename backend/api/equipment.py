"""Fleet overview ("/") and per-equipment detail page (spec sections
21/22)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, get_settings, require_operations
from backend.templating import templates
from db.models import (
    Equipment,
    EquipmentCurrentState,
    EquipmentSnapshot,
    FaultIncident,
    HealthState,
    MonitoringRun,
    User,
)
from monitoring.config import Settings

router = APIRouter()


def _monitoring_status(db: Session, settings: Settings) -> dict:
    last_run = db.execute(select(MonitoringRun).order_by(MonitoringRun.started_at.desc())).scalars().first()

    if last_run is None:
        return {
            "worker_status": "UNKNOWN",
            "target_session": "UNKNOWN",
            "last_run": None,
            "next_poll_estimate": None,
            "poll_interval_seconds": settings.poll_interval_seconds,
        }

    error = last_run.error_message or ""
    if last_run.status.value == "FAILED" and "AUTHENTICATION_REQUIRED" in error:
        target_session = "EXPIRED"
        worker_status = "AUTHENTICATION REQUIRED"
    elif last_run.status.value == "FAILED" and "TARGET_UNAVAILABLE" in error:
        target_session = "UNKNOWN"
        worker_status = "TARGET UNAVAILABLE"
    elif last_run.status.value == "FAILED":
        target_session = "UNKNOWN"
        worker_status = "ERROR"
    else:
        target_session = "AUTHENTICATED"
        worker_status = "RUNNING"

    next_poll_estimate = None
    if last_run.completed_at:
        next_poll_estimate = last_run.completed_at + timedelta(seconds=settings.poll_interval_seconds)

    return {
        "worker_status": worker_status,
        "target_session": target_session,
        "last_run": last_run,
        "next_poll_estimate": next_poll_estimate,
        "poll_interval_seconds": settings.poll_interval_seconds,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    rows = db.execute(
        select(Equipment, EquipmentCurrentState)
        .outerjoin(EquipmentCurrentState, EquipmentCurrentState.equipment_id == Equipment.id)
        .where(Equipment.active.is_(True))
        .order_by(Equipment.name)
    ).all()

    counts = {state.value: 0 for state in HealthState}
    equipment_rows = []
    for equipment, current in rows:
        state = current.health_state if current else HealthState.UNKNOWN
        counts[state.value] += 1
        equipment_rows.append({"equipment": equipment, "current": current, "health_state": state})

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "counts": counts,
            "total": len(equipment_rows),
            "equipment_rows": equipment_rows,
            "monitoring": _monitoring_status(db, settings),
        },
    )


@router.get("/equipment/{equipment_id}", response_class=HTMLResponse)
def equipment_detail(
    equipment_id: int,
    request: Request,
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
):
    equipment = db.get(Equipment, equipment_id)
    if equipment is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": user}, status_code=404)

    current = db.get(EquipmentCurrentState, equipment_id)
    snapshots = db.execute(
        select(EquipmentSnapshot)
        .where(EquipmentSnapshot.equipment_id == equipment_id)
        .order_by(EquipmentSnapshot.observed_at.desc())
        .limit(50)
    ).scalars().all()
    incidents = db.execute(
        select(FaultIncident)
        .where(FaultIncident.equipment_id == equipment_id)
        .order_by(FaultIncident.started_at.desc())
    ).scalars().all()

    active_incident = next((i for i in incidents if i.status.value == "ACTIVE"), None)
    first_fault = incidents[-1] if incidents else None

    return templates.TemplateResponse(
        request,
        "equipment_detail.html",
        {
            "user": user,
            "equipment": equipment,
            "current": current,
            "snapshots": snapshots,
            "incidents": incidents,
            "active_incident": active_incident,
            "first_fault": first_fault,
        },
    )
