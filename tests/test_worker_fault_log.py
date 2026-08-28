"""
Unit tests for monitoring/worker.py's _attach_fault_log_detail: the
best-effort enrichment step that fetches per-component Fault Information
detail (monitoring/lightweight_client.py's get_active_fault_log) for a
just-opened/escalated incident and persists it as FaultLogEntry rows —
see db/models.py's FaultLogEntry and services/alert_engine.py's
_format_fault_log_section, which is what actually consumes these rows.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db.models import FaultLogEntry
from monitoring.models import TargetUnavailableError
from monitoring.worker import _attach_fault_log_detail
from services.health_engine import HealthEngine
from services.state_manager import StateManager
from tests.conftest import make_record

_SAMPLE_ROW = {
    "target_log_id": 21529,
    "component_code": "luozhentanzhenkaiguan",
    "component_description": "Drop cup probe switch Malfunction",
    "is_stop": True,
    "is_clean": False,
    "occurred_at": datetime(2026, 8, 29, 2, 14, 21, tzinfo=timezone.utc),
    "cleared_at": None,
}


class FakeLightweightClient:
    def __init__(self, rows=None, exc=None):
        self._rows = rows or []
        self._exc = exc
        self.requested_device_id: str | None = None

    async def get_active_fault_log(self, device_id: str):
        self.requested_device_id = device_id
        if self._exc:
            raise self._exc
        return self._rows


def _open_incident(db_session):
    sm = StateManager(HealthEngine())
    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    return result.incident_opened


@pytest.mark.asyncio
async def test_attaches_fetched_rows_to_the_incident(db_session):
    incident = _open_incident(db_session)
    client = FakeLightweightClient(rows=[_SAMPLE_ROW])

    await _attach_fault_log_detail(client, db_session, "205", incident)

    assert client.requested_device_id == "205"
    entries = db_session.execute(select(FaultLogEntry).where(FaultLogEntry.incident_id == incident.id)).scalars().all()
    assert len(entries) == 1
    assert entries[0].component_description == "Drop cup probe switch Malfunction"
    assert entries[0].target_log_id == 21529


@pytest.mark.asyncio
async def test_fetch_failure_is_swallowed_not_raised(db_session):
    """The whole point of this being best-effort: a target hiccup at this
    exact moment must never propagate and break the caller (worker.py's
    run_once, which still needs to send the alert)."""
    incident = _open_incident(db_session)
    client = FakeLightweightClient(exc=TargetUnavailableError("target down"))

    await _attach_fault_log_detail(client, db_session, "205", incident)  # must not raise

    entries = db_session.execute(select(FaultLogEntry).where(FaultLogEntry.incident_id == incident.id)).scalars().all()
    assert entries == []


@pytest.mark.asyncio
async def test_no_active_rows_leaves_incident_without_entries(db_session):
    """The common case for most incidents — confirmed live 2026-08-29
    against all 6 real tracked machines (each returned zero active rows
    at that moment)."""
    incident = _open_incident(db_session)
    client = FakeLightweightClient(rows=[])

    await _attach_fault_log_detail(client, db_session, "205", incident)

    entries = db_session.execute(select(FaultLogEntry).where(FaultLogEntry.incident_id == incident.id)).scalars().all()
    assert entries == []
