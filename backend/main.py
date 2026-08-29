"""
FastAPI app: the Phase 5 web dashboard. Server-rendered (Jinja2), not a
separate Next.js SPA — see README for why. Reads from the same Postgres
database monitoring/worker.py writes to; does not talk to the target
application or Playwright at all (spec section 30: don't run Playwright
in the request process).

Run:
    uvicorn backend.main:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api import alert_recipients, alerts, auth, costs, equipment, health, inventory, monitoring, orders, staff, users, venues
from backend.deps import NotAuthenticated
from db import base as db_base
from monitoring.config import load_settings

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()
    yield


app = FastAPI(title="Equipment Health Monitoring", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


app.include_router(auth.router)
app.include_router(equipment.router)
app.include_router(alerts.router)
app.include_router(users.router)
app.include_router(monitoring.router)
app.include_router(orders.router)
app.include_router(costs.router)
app.include_router(staff.router)
app.include_router(alert_recipients.router)
app.include_router(health.router)
app.include_router(inventory.router)
app.include_router(venues.router)
