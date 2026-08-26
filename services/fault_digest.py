"""
Critical Faults Digest: a single email listing EVERY machine currently
in a Critical-severity active incident (MALFUNCTION or OFFLINE), as a
table — distinct from services/alert_engine.py's per-incident instant
alerts (fired once when a fault is first detected/escalated/resolved,
one machine per email). This is a rollup/summary view of "what's wrong
right now", built and sent on demand (see send_fault_digest.py's CLI).

Per explicit request: always tabular, with fault details — both a
plain-text aligned table (the fallback MIME part for clients that don't
render HTML, and what the console channel logs when SMTP isn't
configured) and a proper HTML <table> (what actually renders in a real
inbox, via NotificationService's html_body support).

Recipients reuse AlertEngine.get_recipients(session, equipment_id=None,
severity="Critical") — the same admins-always + AlertRecipient-always +
"subscribed to all machines" audience that already governs every other
Critical-severity notification in this app, rather than inventing a
second recipient-resolution path. Scope note: a user subscribed to one
specific machine only (not "all machines") is not included here even if
that machine happens to be one of the currently-faulting ones — a
deliberate choice for a rollup digest, not an oversight.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Equipment, FaultIncident, IncidentStatus
from orders.mapping import format_ist
from services.alert_engine import AlertEngine

CRITICAL_SEVERITY = "Critical"

_COLUMNS = ("Machine", "Equipment ID", "Equipment Code", "Fault", "Health", "Since (IST)", "Duration")


@dataclass
class FaultDigest:
    subject: str
    plain_body: str
    html_body: str
    recipients: list[str]
    incident_count: int


def _fetch_active_critical(session: Session) -> list[tuple[FaultIncident, Equipment]]:
    return session.execute(
        select(FaultIncident, Equipment)
        .join(Equipment, FaultIncident.equipment_id == Equipment.id)
        .where(FaultIncident.status == IncidentStatus.ACTIVE, FaultIncident.severity == CRITICAL_SEVERITY)
        .order_by(FaultIncident.started_at.asc())
    ).all()


def _duration(started_at: datetime) -> str:
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - started
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes = remainder // 60
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _row_values(inc: FaultIncident, eq: Equipment) -> tuple[str, ...]:
    return (
        eq.name, eq.external_id, eq.equipment_code, inc.fault_type,
        inc.current_health.value, format_ist(inc.started_at), _duration(inc.started_at),
    )


def _plain_text_table(rows: list[tuple[FaultIncident, Equipment]]) -> str:
    table_rows = [_row_values(inc, eq) for inc, eq in rows]
    widths = [max(len(str(h)), *(len(str(r[i])) for r in table_rows)) for i, h in enumerate(_COLUMNS)]

    def _line(cells: tuple) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))

    lines = [_line(_COLUMNS), _line(tuple("-" * w for w in widths))]
    lines.extend(_line(r) for r in table_rows)
    return "\n".join(lines)


def _html_table(rows: list[tuple[FaultIncident, Equipment]]) -> str:
    header_cells = "".join(
        f'<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #dc2626;">{html.escape(h)}</th>'
        for h in _COLUMNS
    )
    body_rows = "".join(
        "<tr>" + "".join(
            f'<td style="padding:6px 10px;border-bottom:1px solid #e5e7eb;">{html.escape(str(v))}</td>'
            for v in _row_values(inc, eq)
        ) + "</tr>"
        for inc, eq in rows
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1d23;">'
        '<h2 style="color:#dc2626;margin-bottom:4px;">Critical Faults Report</h2>'
        f"<p>{len(rows)} machine(s) currently in a Critical (Malfunction/Offline) state.</p>"
        '<table style="border-collapse:collapse;width:100%;">'
        f"<thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody>"
        "</table></div>"
    )


def build_digest(session: Session, alert_engine: AlertEngine) -> FaultDigest | None:
    """Returns None when there are no active Critical incidents right
    now — an empty "all clear" digest isn't useful and would just train
    recipients to start ignoring these emails."""
    rows = _fetch_active_critical(session)
    if not rows:
        return None

    recipients = alert_engine.get_recipients(session, equipment_id=None, severity=CRITICAL_SEVERITY)
    subject = f"Critical Faults Report — {len(rows)} machine(s) affected"
    plain_body = (
        f"{len(rows)} machine(s) are currently in a Critical (Malfunction/Offline) state:\n\n"
        + _plain_text_table(rows)
    )

    return FaultDigest(
        subject=subject, plain_body=plain_body, html_body=_html_table(rows),
        recipients=recipients, incident_count=len(rows),
    )
