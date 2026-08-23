"""
AlertEngine: decides WHO gets notified and WHAT the message says, for the
incident events StateManager already detected (services/state_manager.py).
Detection/dedup correctness lives there — this module must never re-derive
"is this a new fault", only react to the ObservationResult it's given.

Recipients (project requirement #19):
  - Global administrators (User.role == "admin", active) always receive
    Critical-severity alerts, regardless of any subscription row.
  - Everyone else is reached only via an enabled AlertSubscription row
    matching this equipment (or a NULL equipment_id = "all machines") and
    this severity (or a NULL severity = "all severities").
Recipients are de-duplicated by email before sending (one email per
person even if multiple subscriptions match).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AlertSubscription, Equipment, FaultIncident, User
from monitoring.config import Settings
from services.notification_service import NotificationService
from services.state_manager import ObservationResult

logger = logging.getLogger(__name__)


def _format_incident_message(equipment: Equipment, incident: FaultIncident) -> tuple[str, str]:
    """Matches the example format in spec section 18. Uses the incident's
    preserved exact fault_type (requirement #10), never a generic label."""
    subject = f"Equipment Malfunction — {equipment.name} / {equipment.external_id}"
    body = (
        f"Equipment:\n{equipment.name}\n\n"
        f"Equipment ID:\n{equipment.external_id}\n\n"
        f"Equipment Code:\n{equipment.equipment_code}\n\n"
        f"Health:\n{incident.current_health.value}\n\n"
        f"Fault:\n{incident.fault_type}\n\n"
        f"Severity:\n{incident.severity}\n\n"
        f"Detected:\n{incident.started_at.strftime('%d %b %Y %H:%M:%S')}"
    )
    return subject, body


def _format_escalation_message(equipment: Equipment, incident: FaultIncident) -> tuple[str, str]:
    subject = f"Equipment Fault Escalated — {equipment.name} / {equipment.external_id}"
    body = (
        f"Equipment:\n{equipment.name}\n\n"
        f"Equipment ID:\n{equipment.external_id}\n\n"
        f"Now:\n{incident.current_health.value}\n\n"
        f"Fault:\n{incident.fault_type}\n\n"
        f"Severity:\n{incident.severity}\n\n"
        f"Ongoing since:\n{incident.started_at.strftime('%d %b %Y %H:%M:%S')}"
    )
    return subject, body


def _format_recovery_message(equipment: Equipment, incident: FaultIncident) -> tuple[str, str]:
    downtime = (incident.resolved_at or datetime.now(timezone.utc)) - incident.started_at
    subject = f"Equipment Recovered — {equipment.name} / {equipment.external_id}"
    body = (
        f"Equipment:\n{equipment.name}\n\n"
        f"Equipment ID:\n{equipment.external_id}\n\n"
        f"Fault that resolved:\n{incident.fault_type}\n\n"
        f"Started:\n{incident.started_at.strftime('%d %b %Y %H:%M:%S')}\n\n"
        f"Resolved:\n{incident.resolved_at.strftime('%d %b %Y %H:%M:%S')}\n\n"
        f"Downtime:\n{downtime}"
    )
    return subject, body


class AlertEngine:
    def __init__(self, settings: Settings, notification_service: NotificationService):
        self._settings = settings
        self._notifications = notification_service

    def get_recipients(self, session: Session, equipment_id: int, severity: str) -> list[str]:
        emails: set[str] = set()

        admins = session.execute(
            select(User).where(User.role == "admin", User.active.is_(True))
        ).scalars().all()
        if severity == "Critical":
            emails.update(a.email for a in admins)

        subs = session.execute(
            select(AlertSubscription, User)
            .join(User, AlertSubscription.user_id == User.id)
            .where(
                AlertSubscription.enabled.is_(True),
                AlertSubscription.notification_type == "email",
                User.active.is_(True),
                (AlertSubscription.equipment_id.is_(None)) | (AlertSubscription.equipment_id == equipment_id),
                (AlertSubscription.severity.is_(None)) | (AlertSubscription.severity == severity),
            )
        ).all()
        emails.update(user.email for _sub, user in subs)

        return sorted(emails)

    def notify(self, session: Session, result: ObservationResult) -> None:
        equipment = result.equipment

        if result.incident_opened:
            incident = result.incident_opened
            recipients = self.get_recipients(session, equipment.id, incident.severity)
            subject, body = _format_incident_message(equipment, incident)
            self._send(recipients, subject, body, incident, "new incident")

        elif result.incident_escalated and self._settings.alert_on_escalation:
            incident = result.incident_escalated
            recipients = self.get_recipients(session, equipment.id, incident.severity)
            subject, body = _format_escalation_message(equipment, incident)
            self._send(recipients, subject, body, incident, "escalation")

        elif result.incident_resolved and self._settings.send_recovery_notifications:
            incident = result.incident_resolved
            # Recovery goes to the same audience the original fault would
            # have reached (severity it was at when it resolved).
            recipients = self.get_recipients(session, equipment.id, incident.severity)
            subject, body = _format_recovery_message(equipment, incident)
            self._send(recipients, subject, body, incident, "recovery", mark_sent=False)

    def _send(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        incident: FaultIncident,
        kind: str,
        mark_sent: bool = True,
    ) -> None:
        if not recipients:
            logger.warning("No recipients found for %s alert on incident %s — nothing sent", kind, incident.id)
            return
        sent = self._notifications.send_email(recipients, subject, body)
        if sent and mark_sent:
            incident.notification_sent = True
        logger.info("%s alert for incident %s: sent=%s recipients=%s", kind, incident.id, sent, recipients)
