"""
Staff & Leave Management tab (admin-only): a staff roster (employment
start/end date), leaves logged by the admin on a staff member's behalf,
and a monthly leave summary that explicitly highlights anyone with more
than 2 leave days in the selected month (staff/leave_summary.py).
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.templating import templates
from db.models import Staff, StaffLeave, User
from staff.leave_summary import summarize_month

router = APIRouter()


@router.get("/staff", response_class=HTMLResponse)
def staff_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = request.query_params
    today = date_cls.today()
    try:
        year = int(q.get("year", today.year))
        month = int(q.get("month", today.month))
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = today.year, today.month

    staff_list = db.execute(select(Staff).order_by(Staff.name)).scalars().all()
    leaves = db.execute(select(StaffLeave)).scalars().all()
    leave_summary = summarize_month(staff_list, leaves, year, month)

    recent_leaves = db.execute(
        select(StaffLeave, Staff)
        .join(Staff, StaffLeave.staff_id == Staff.id)
        .order_by(StaffLeave.start_date.desc())
        .limit(30)
    ).all()

    return templates.TemplateResponse(
        request,
        "staff.html",
        {
            "user": admin,
            "staff_list": staff_list,
            "active_staff": [s for s in staff_list if not s.employment_end_date],
            "leave_summary": leave_summary,
            "recent_leaves": recent_leaves,
            "year": year,
            "month": month,
            "today": today.isoformat(),
            # Datalist suggestions only -- free text, not enforced, so
            # existing department names stay consistent without forcing
            # a fixed list up front.
            "known_departments": sorted({s.department for s in staff_list if s.department}),
            "known_sub_departments": sorted({s.sub_department for s in staff_list if s.sub_department}),
        },
    )


@router.post("/staff")
def create_staff(
    name: str = Form(...),
    department: str = Form(""),
    sub_department: str = Form(""),
    employment_start_date: str = Form(...),
    employment_end_date: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    db.add(
        Staff(
            name=name.strip(),
            department=department.strip() or None,
            sub_department=sub_department.strip() or None,
            employment_start_date=employment_start_date,
            employment_end_date=employment_end_date.strip() or None,
        )
    )
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/{staff_id}/offboard")
def offboard_staff(
    staff_id: int,
    end_date: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(Staff, staff_id)
    if target is not None:
        target.employment_end_date = end_date
    return RedirectResponse(url="/staff", status_code=303)


@router.get("/staff/{staff_id}/edit", response_class=HTMLResponse)
def edit_staff_form(
    staff_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    staff = db.get(Staff, staff_id)
    if staff is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": admin}, status_code=404)

    all_staff = db.execute(select(Staff)).scalars().all()
    return templates.TemplateResponse(
        request,
        "staff_edit.html",
        {
            "user": admin,
            "staff": staff,
            "known_departments": sorted({s.department for s in all_staff if s.department}),
            "known_sub_departments": sorted({s.sub_department for s in all_staff if s.sub_department}),
        },
    )


@router.post("/staff/{staff_id}/edit")
def update_staff(
    staff_id: int,
    name: str = Form(...),
    department: str = Form(""),
    sub_department: str = Form(""),
    employment_start_date: str = Form(...),
    employment_end_date: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    staff = db.get(Staff, staff_id)
    if staff is None:
        return RedirectResponse(url="/staff", status_code=303)

    staff.name = name.strip()
    staff.department = department.strip() or None
    staff.sub_department = sub_department.strip() or None
    staff.employment_start_date = employment_start_date
    # Blank end date on edit is exactly how a mistaken "mark as left" gets
    # undone -- clearing it here makes the staff member active again, and
    # the roster's existing "not s.employment_end_date" check is what
    # brings the "Mark as left" action back into view for them.
    staff.employment_end_date = employment_end_date.strip() or None
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/leaves")
def add_leave(
    staff_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    reason: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Never store an inverted range -- swap rather than reject, since a
    # typo'd order shouldn't lose the admin's input.
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    db.add(
        StaffLeave(
            staff_id=staff_id,
            start_date=start_date,
            end_date=end_date,
            reason=reason.strip() or None,
            created_by_user_id=admin.id,
        )
    )
    return RedirectResponse(url="/staff", status_code=303)
