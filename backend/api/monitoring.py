"""JSON monitoring-status endpoint (spec section 24) — the same data the
dashboard's status panel shows, exposed as JSON for programmatic use /
a future non-HTML frontend."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.equipment import _monitoring_status
from backend.deps import get_db, get_settings, require_operations
from db.models import User
from monitoring.config import Settings

router = APIRouter(prefix="/api")


@router.get("/monitoring/status")
def monitoring_status(
    user: User = Depends(require_operations),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    status = _monitoring_status(db, settings)
    last_run = status.pop("last_run")
    return {
        **status,
        "last_run": None
        if last_run is None
        else {
            "id": last_run.id,
            "started_at": last_run.started_at.isoformat(),
            "completed_at": last_run.completed_at.isoformat() if last_run.completed_at else None,
            "status": last_run.status.value,
            "records_found": last_run.records_found,
            "records_processed": last_run.records_processed,
            "error_message": last_run.error_message,
        },
        "next_poll_estimate": status["next_poll_estimate"].isoformat() if status["next_poll_estimate"] else None,
    }
