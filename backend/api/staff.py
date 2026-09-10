"""
Staff & Leave Management tab (admin-only): a staff roster (employment
start/end date), leaves logged by the admin on a staff member's behalf,
a monthly leave summary that explicitly highlights anyone with more
than 2 leave days in the selected month (staff/leave_summary.py), and
an advance-payments ledger.
"""
from __future__ import annotations

from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.deps import get_db, require_admin
from backend.templating import templates
from db.models import AlertRecipient, Staff, StaffAdvance, StaffLeave, User
from staff.leave_summary import summarize_month

router = APIRouter()


def _deactivate_alert_recipient_for(db: Session, email: str | None) -> None:
    """A staff member with a set employment_end_date has left -- per
    explicit request, stop any equipment alerts they were receiving via
    their staff email being added as a flat AlertRecipient
    (backend/api/alert_recipients.py). Soft-deactivate (matches
    AlertRecipient's own activate/deactivate lifecycle) rather than
    delete, so the row/history isn't lost and an admin can see it was
    auto-turned-off rather than never having existed. A no-op if no email
    was set, or no matching (or already-inactive) recipient exists."""
    if not email:
        return
    recipient = db.execute(
        select(AlertRecipient).where(func.lower(AlertRecipient.email) == email.lower())
    ).scalar_one_or_none()
    if recipient is not None and recipient.active:
        recipient.active = False


def _parse_optional_decimal(raw: str) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


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

    recent_advances = db.execute(
        select(StaffAdvance, Staff)
        .join(Staff, StaffAdvance.staff_id == Staff.id)
        .order_by(StaffAdvance.date.desc())
        .limit(30)
    ).all()
    advance_totals = dict(
        db.execute(select(StaffAdvance.staff_id, func.sum(StaffAdvance.amount)).group_by(StaffAdvance.staff_id)).all()
    )

    return templates.TemplateResponse(
        request,
        "staff.html",
        {
            "user": admin,
            "staff_list": staff_list,
            "active_staff": [s for s in staff_list if not s.employment_end_date],
            "leave_summary": leave_summary,
            "recent_leaves": recent_leaves,
            "recent_advances": recent_advances,
            "advance_totals": advance_totals,
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
    email: str = Form(""),
    monthly_salary: str = Form(""),
    employment_start_date: str = Form(...),
    employment_end_date: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    resolved_email = email.strip().lower() or None
    resolved_end_date = employment_end_date.strip() or None
    db.add(
        Staff(
            name=name.strip(),
            department=department.strip() or None,
            sub_department=sub_department.strip() or None,
            email=resolved_email,
            monthly_salary=_parse_optional_decimal(monthly_salary),
            employment_start_date=employment_start_date,
            employment_end_date=resolved_end_date,
        )
    )
    # Covers the rare case of adding a historical/backdated record for
    # someone who has already left -- their email (if any) shouldn't be
    # left as an active alert recipient either.
    if resolved_end_date:
        _deactivate_alert_recipient_for(db, resolved_email)
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
        # Per explicit request: a staff member who has left stops
        # receiving equipment alerts if their email was ever added to
        # the flat Alert Recipients list.
        _deactivate_alert_recipient_for(db, target.email)
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
    email: str = Form(""),
    monthly_salary: str = Form(""),
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
    staff.email = email.strip().lower() or None
    staff.monthly_salary = _parse_optional_decimal(monthly_salary)
    staff.employment_start_date = employment_start_date
    # Blank end date on edit is exactly how a mistaken "mark as left" gets
    # undone -- clearing it here makes the staff member active again, and
    # the roster's existing "not s.employment_end_date" check is what
    # brings the "Mark as left" action back into view for them. Note this
    # does NOT automatically re-activate a previously-auto-deactivated
    # alert recipient below -- that's a deliberate admin decision, not
    # something undoing an end-date should silently do on its own.
    staff.employment_end_date = employment_end_date.strip() or None
    # Per explicit request: a staff member who has left (via this edit
    # form, not just the dedicated "Mark as left" action) stops receiving
    # equipment alerts if their email was ever added to the flat Alert
    # Recipients list.
    if staff.employment_end_date:
        _deactivate_alert_recipient_for(db, staff.email)
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/leaves")
def add_leave(
    staff_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    is_half_day: str = Form(""),
    reason: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    half_day = bool(is_half_day)
    if half_day:
        # A half-day leave is always a single day -- the end date, if the
        # form somehow submitted a different one, is not meaningful here.
        end_date = start_date
    # Never store an inverted range -- swap rather than reject, since a
    # typo'd order shouldn't lose the admin's input.
    elif end_date < start_date:
        start_date, end_date = end_date, start_date

    db.add(
        StaffLeave(
            staff_id=staff_id,
            start_date=start_date,
            end_date=end_date,
            is_half_day=half_day,
            reason=reason.strip() or None,
            created_by_user_id=admin.id,
        )
    )
    return RedirectResponse(url="/staff", status_code=303)


@router.get("/staff/leaves/{leave_id}/edit", response_class=HTMLResponse)
def edit_leave_form(
    leave_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    leave = db.get(StaffLeave, leave_id)
    if leave is None:
        return templates.TemplateResponse(request, "not_found.html", {"user": admin}, status_code=404)

    staff_list = db.execute(select(Staff).order_by(Staff.name)).scalars().all()
    return templates.TemplateResponse(
        request,
        "staff_leave_edit.html",
        {"user": admin, "leave": leave, "staff_list": staff_list},
    )


@router.post("/staff/leaves/{leave_id}/edit")
def update_leave(
    leave_id: int,
    staff_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    is_half_day: str = Form(""),
    reason: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    leave = db.get(StaffLeave, leave_id)
    if leave is None:
        return RedirectResponse(url="/staff", status_code=303)

    half_day = bool(is_half_day)
    if half_day:
        end_date = start_date
    elif end_date < start_date:
        start_date, end_date = end_date, start_date

    leave.staff_id = staff_id
    leave.start_date = start_date
    leave.end_date = end_date
    leave.is_half_day = half_day
    leave.reason = reason.strip() or None
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/leaves/{leave_id}/delete")
def delete_leave(
    leave_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    leave = db.get(StaffLeave, leave_id)
    if leave is not None:
        db.delete(leave)
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/{staff_id}/delete")
def delete_staff(
    staff_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Staff.leaves and Staff.advances both have cascade="all,
    # delete-orphan" -- an ORM delete here already correctly removes
    # every leave/advance row for this staff member too. StaffLeave.staff_id
    # is the only other FK anywhere pointing at staff.id (confirmed via a
    # repo-wide search), so a hard delete is safe.
    staff = db.get(Staff, staff_id)
    if staff is not None:
        db.delete(staff)
    return RedirectResponse(url="/staff", status_code=303)


@router.post("/staff/advances")
def add_advance(
    staff_id: int = Form(...),
    amount: str = Form(...),
    date: str = Form(...),
    note: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        resolved_amount = Decimal(amount)
    except InvalidOperation:
        resolved_amount = Decimal("0")

    db.add(
        StaffAdvance(
            staff_id=staff_id,
            amount=resolved_amount,
            date=date,
            note=note.strip() or None,
            created_by_user_id=admin.id,
        )
    )
    return RedirectResponse(url="/staff", status_code=303)
