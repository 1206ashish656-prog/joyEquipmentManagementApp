"""Public, unauthenticated health check for the hosting platform's HTTP
health check. Deliberately reveals nothing about equipment/worker state
(see the authenticated /api/monitoring/status in backend/api/monitoring.py
for that) — this only proves the web process itself is up and accepting
requests. No DB ping needed: backend/main.py's lifespan() already calls
db_base.init_engine()/create_all() at process startup, so a Postgres
outage fails the process to start at all, which is an adequate implicit
check for this scope."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
