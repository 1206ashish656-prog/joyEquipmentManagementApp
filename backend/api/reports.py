"""
Senior Management Report (admin-only, /reports/management): per
explicit request, this is now a control panel, not an on-screen report
-- pick a period + optional machine, click Generate, and download the
PDF once it's ready. The actual computation
(services/management_report.py) and PDF rendering
(services/report_pdf.py, services/chart_svg.py) run entirely in the
background worker process (services/report_job_worker.py, wired into
monitoring/combined_worker.py) -- "an isolated report generation
process." This module only ever creates a ReportJob row (PENDING) and
later reads a finished one back; it never computes a report or
launches a browser itself.
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.period_utils import PERIODS, period_range
from backend.templating import templates
from db.models import Equipment, ReportJob, ReportJobStatus, User

router = APIRouter()

_IN_PROGRESS_STATUSES = (ReportJobStatus.PENDING, ReportJobStatus.RUNNING)


def _resolve_equipment(db: Session, raw_equipment_id: str) -> Equipment | None:
    raw_equipment_id = (raw_equipment_id or "").strip()
    if not raw_equipment_id:
        return None
    try:
        equipment_id = int(raw_equipment_id)
    except ValueError:
        return None
    return db.get(Equipment, equipment_id)


@router.get("/reports/management", response_class=HTMLResponse)
def report_jobs_page(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    equipment_list = db.execute(select(Equipment).order_by(Equipment.name)).scalars().all()
    jobs = db.execute(
        select(ReportJob, User)
        .outerjoin(User, ReportJob.requested_by_user_id == User.id)
        .order_by(ReportJob.created_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "report_jobs.html",
        {
            "user": admin,
            "equipment_list": equipment_list,
            "jobs": jobs,
            # Auto-refreshes the page (see the template's head_extra
            # block) only while something is actually in flight -- no
            # point polling a page where nothing is going to change.
            "has_in_progress": any(job.status in _IN_PROGRESS_STATUSES for job, _ in jobs),
            "periods": PERIODS,
            "today": date_cls.today().isoformat(),
        },
    )


@router.post("/reports/management/jobs")
def create_report_job(
    period: str = Form("monthly"),
    as_of: str = Form(""),
    start: str = Form(""),
    end: str = Form(""),
    equipment_id: str = Form(""),
    include_datewise_sales: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if period not in PERIODS:
        period = "monthly"
    resolved_as_of = as_of or date_cls.today().isoformat()
    resolved_start, resolved_end = period_range(period, resolved_as_of, start=start, end=end)
    equipment = _resolve_equipment(db, equipment_id)

    db.add(ReportJob(
        requested_by_user_id=admin.id,
        period=period,
        as_of=resolved_as_of,
        start=resolved_start,
        end=resolved_end,
        equipment_id=equipment.id if equipment else None,
        include_datewise_sales=bool(include_datewise_sales),
        status=ReportJobStatus.PENDING,
    ))
    return RedirectResponse(url="/reports/management", status_code=303)


@router.get("/reports/management/jobs/{job_id}/download")
def download_report_job(job_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    job = db.get(ReportJob, job_id)
    if job is None or job.status != ReportJobStatus.SUCCESS or not job.pdf_data:
        return RedirectResponse(url="/reports/management", status_code=303)

    scope_slug = "All_Machines"
    if job.equipment_id is not None:
        equipment = db.get(Equipment, job.equipment_id)
        if equipment is not None:
            scope_slug = equipment.name.replace(" ", "_")
    filename = f"management-report_{job.start}_to_{job.end}_{scope_slug}.pdf"

    return Response(
        content=job.pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
