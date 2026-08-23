"""
StateManager: implements requirement #8/#9 — detect a NEW malfunction
exactly once (not once per poll), track it as a single incident for its
entire duration, and detect recovery.

Persists: equipment master (upsert), equipment_current_state (the
dashboard's "now" view), equipment_snapshot (history/timeline), and
fault_incident (the alerting/audit trail). Does NOT send notifications —
that's Phase 4 (services/alert_engine.py); this module just returns
which incidents were opened/escalated/resolved so an alert engine can act
on that later.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (
    Equipment,
    EquipmentCurrentState,
    EquipmentSnapshot,
    FaultIncident,
    HealthState,
    IncidentStatus,
    FAULT_STATES,
)
from monitoring.models import EquipmentRecord
from services.health_engine import HealthEngine, HealthEvaluation


@dataclass
class ObservationResult:
    equipment: Equipment
    evaluation: HealthEvaluation
    state_changed: bool
    incident_opened: FaultIncident | None = None
    incident_escalated: FaultIncident | None = None
    incident_resolved: FaultIncident | None = None


def _get_or_create_equipment(session: Session, record: EquipmentRecord) -> Equipment:
    equipment = session.execute(
        select(Equipment).where(Equipment.external_id == record.equipment_id)
    ).scalar_one_or_none()

    if equipment is None:
        equipment = Equipment(
            external_id=record.equipment_id,
            equipment_code=record.equipment_code,
            name=record.name,
            device_type=record.device_type,
            device_address=record.device_address,
            advertising_group=record.advertising_group,
        )
        session.add(equipment)
        session.flush()  # assign equipment.id
    else:
        # Equipment master fields can legitimately change (renamed, moved,
        # re-coded) — keep them current. Health/state fields are handled
        # separately below; this is just the "master data" part.
        equipment.equipment_code = record.equipment_code
        equipment.name = record.name
        equipment.device_type = record.device_type
        equipment.device_address = record.device_address
        equipment.advertising_group = record.advertising_group

    return equipment


def _get_open_incident(session: Session, equipment_id: int) -> FaultIncident | None:
    return session.execute(
        select(FaultIncident)
        .where(FaultIncident.equipment_id == equipment_id, FaultIncident.status == IncidentStatus.ACTIVE)
        .order_by(FaultIncident.started_at.desc())
    ).scalars().first()


class StateManager:
    def __init__(self, health_engine: HealthEngine):
        self._health_engine = health_engine

    def process_observation(self, session: Session, record: EquipmentRecord) -> ObservationResult:
        evaluation = self._health_engine.evaluate(record)
        observed_at: datetime = record.observed_at

        equipment = _get_or_create_equipment(session, record)

        # History: always recorded, regardless of whether health changed —
        # this is what powers the equipment detail timeline (requirement #22).
        session.add(
            EquipmentSnapshot(
                equipment_id=equipment.id,
                status=record.status,
                network_status=record.network_status,
                fault_type=record.fault_type,
                material_shortage_status=record.material_shortage_status,
                remaining_oranges=record.remaining_oranges,
                health_state=evaluation.state,
                observed_at=observed_at,
            )
        )

        current = session.get(EquipmentCurrentState, equipment.id)
        previous_health = current.health_state if current else None
        state_changed = previous_health != evaluation.state

        result = self._apply_transition(session, equipment, previous_health, evaluation, record, observed_at)

        if current is None:
            current = EquipmentCurrentState(equipment_id=equipment.id, last_changed_at=observed_at)
            session.add(current)
        current.health_state = evaluation.state
        current.status = record.status
        current.network_status = record.network_status
        current.fault_type = record.fault_type
        current.material_shortage_status = record.material_shortage_status
        current.remaining_oranges = record.remaining_oranges
        current.last_seen_at = observed_at
        if state_changed:
            current.last_changed_at = observed_at

        session.flush()

        result.state_changed = state_changed
        return result

    def _apply_transition(
        self,
        session: Session,
        equipment: Equipment,
        previous_health: HealthState | None,
        evaluation: HealthEvaluation,
        record: EquipmentRecord,
        observed_at: datetime,
    ) -> ObservationResult:
        was_fault = previous_health in FAULT_STATES if previous_health else False
        is_fault = evaluation.state in FAULT_STATES
        is_healthy = evaluation.state == HealthState.HEALTHY

        result = ObservationResult(equipment=equipment, evaluation=evaluation, state_changed=False)

        if is_fault and not was_fault:
            # Requirement #8: exactly ONE incident is created when a
            # malfunction/warning/offline state FIRST appears.
            incident = FaultIncident(
                equipment_id=equipment.id,
                fault_type=record.fault_type,  # exact raw text (requirement #10)
                severity=self._health_engine.severity_for(evaluation.state),
                previous_health=previous_health,
                current_health=evaluation.state,
                started_at=observed_at,
                status=IncidentStatus.ACTIVE,
                notification_sent=False,
            )
            session.add(incident)
            session.flush()
            result.incident_opened = incident

        elif is_fault and was_fault:
            # Still faulty — requirement #8 explicitly forbids a new alert
            # per poll. Update the SAME open incident (covers e.g.
            # WARNING -> MALFUNCTION escalation, or the raw fault text
            # changing) rather than creating a second one.
            open_incident = _get_open_incident(session, equipment.id)
            if open_incident is not None:
                escalated = open_incident.current_health != evaluation.state
                open_incident.current_health = evaluation.state
                open_incident.fault_type = record.fault_type
                open_incident.severity = self._health_engine.severity_for(evaluation.state)
                if escalated:
                    result.incident_escalated = open_incident

        elif is_healthy and was_fault:
            # Requirement #9: explicit recovery only — never inferred from
            # UNKNOWN/uncertain data (see the `elif`s below: UNKNOWN never
            # reaches this branch).
            open_incident = _get_open_incident(session, equipment.id)
            if open_incident is not None:
                open_incident.status = IncidentStatus.RESOLVED
                open_incident.resolved_at = observed_at
                result.incident_resolved = open_incident

        # else: e.g. evaluation.state is UNKNOWN, or HEALTHY -> HEALTHY, or
        # UNKNOWN -> UNKNOWN. Deliberately no incident action — requirement
        # #28: uncertainty must never open, escalate, or resolve an
        # incident.

        return result
