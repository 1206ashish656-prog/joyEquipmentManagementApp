"""
Send the Critical Faults Digest right now — a single tabular email
listing every machine currently in a Critical-severity active incident.
See services/fault_digest.py for what it contains and who it goes to.

Sends nothing (and exits with a clear message) if no machine is
currently Critical — this is an on-demand snapshot, not a scheduled
job, so running it during a genuinely healthy fleet is a normal,
expected outcome, not an error.

Run:
    python -m services.send_fault_digest
"""
from __future__ import annotations

import logging

from db import base as db_base
from monitoring.config import load_settings
from services.alert_engine import AlertEngine
from services.fault_digest import build_digest
from services.notification_service import NotificationService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("services.send_fault_digest")


def main() -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    notification_service = NotificationService(settings)
    alert_engine = AlertEngine(settings, notification_service)

    with db_base.get_session() as session:
        digest = build_digest(session, alert_engine)
        if digest is None:
            print("No machines are currently in a Critical state — nothing to send.")
            return

        print(f"{digest.incident_count} machine(s) currently Critical. Recipients: {', '.join(digest.recipients)}")
        sent = notification_service.send_email(
            digest.recipients, digest.subject, digest.plain_body, html_body=digest.html_body
        )
        print(f"Send result: {'sent' if sent else 'FAILED — see log above'}")


if __name__ == "__main__":
    main()
