"""
Machine server <-> PayU PGW reconciliation (admin-only, /reconciliation)
-- a separate endpoint from Cost Management/Order Summary, per explicit
request. Compares OrderPaymentRecord (individual machine orders,
db/models.py) against PayU's own transaction records
(services/payu_client.py) for a chosen date range, via
services/reconciliation.py's pure matching logic.

The PayU call is a single fast REST POST (no browser/Playwright
involved, unlike the management report's PDF export) so it runs
synchronously in the request -- no background-job queue needed here.
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.deps import get_db, get_settings, require_admin
from backend.templating import templates
from db.models import OrderPaymentRecord, User
from monitoring.config import Settings
from monitoring.models import MonitoringError
from services.payu_client import PayUClient, PayUNotConfiguredError
from services.reconciliation import build_reconciliation

router = APIRouter()


@router.get("/reconciliation", response_class=HTMLResponse)
async def reconciliation_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    q = request.query_params
    today = date_cls.today().isoformat()
    start = q.get("start") or today
    end = q.get("end") or today
    if end < start:
        start, end = end, start

    context = {
        "user": admin,
        "start": start,
        "end": end,
        "payu_configured": settings.payu_configured,
        "report": None,
        "error": None,
        "ran": False,
    }

    # Only actually calls PayU when the admin explicitly asks (?run=1) --
    # same "nothing silently calls an external paid API" caution as
    # every other admin-triggered action in this app.
    if settings.payu_configured and q.get("run") == "1":
        context["ran"] = True
        machine_orders = db.execute(
            select(OrderPaymentRecord).where(
                OrderPaymentRecord.order_date >= start, OrderPaymentRecord.order_date <= end,
            )
        ).scalars().all()

        client = PayUClient(settings)
        try:
            payu_transactions = await client.get_transaction_details(start, end)
        except PayUNotConfiguredError as e:
            context["error"] = str(e)
        except MonitoringError as e:
            context["error"] = f"Could not fetch PayU transactions ({e.state.value}): {e}"
        else:
            context["report"] = build_reconciliation(machine_orders, payu_transactions, start, end)
        finally:
            await client.close()

    return templates.TemplateResponse(request, "reconciliation.html", context)
