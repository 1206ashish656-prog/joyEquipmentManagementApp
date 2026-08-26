"""
Operational utility: add or remove a TEST equipment row through the exact
same code path the live poller uses (StateManager.process_observation),
so it behaves identically to a real machine for dashboard display,
incident tracking, and (optionally) alert emails.

Intended audience: the application management team, via
docs/OPERATIONS_RUNBOOK.md — not meant to be exotic or require reading
this file. Every equipment_id created this way is expected to start with
"TEST-" (enforced) so it's always obviously safe to `remove` later and
never mistaken for a real machine on the dashboard.

Examples:
    # Add a healthy test machine
    python -m services.simulate_equipment_event add --id TEST-001 --state healthy

    # Add (or transition an existing one to) a malfunction, and send the
    # real alert email for it (only fires if this is a NEW fault --
    # matches production behaviour, see services/state_manager.py)
    python -m services.simulate_equipment_event add --id TEST-001 --state malfunction --notify

    # Clean up afterward
    python -m services.simulate_equipment_event remove --id TEST-001
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import datetime, timezone

from db import base as db_base
from db.models import Equipment
from monitoring.config import load_settings
from monitoring.models import EquipmentRecord
from services.alert_engine import AlertEngine
from services.health_engine import HealthEngine
from services.notification_service import NotificationService
from services.state_manager import StateManager

logger = logging.getLogger("services.simulate_equipment_event")

# Maps a plain-English --state onto the raw fields HealthEngine actually
# evaluates (config/health_rules.yaml) -- so the caller never needs to know
# that vocabulary, just the outcome they want.
_STATE_PRESETS = {
    "healthy": dict(status="Normal", network_status="Online", fault_type="Normal", material_shortage_status="Normal"),
    "warning": dict(status="Warning", network_status="Online", fault_type="Normal", material_shortage_status="Low"),
    "malfunction": dict(status="Fault", network_status="Online", fault_type="Simulated Fault", material_shortage_status="Normal"),
    "offline": dict(status="Offline", network_status="Offline", fault_type="Normal", material_shortage_status="Normal"),
}


def _default_code(equipment_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", equipment_id).upper()


def add(args: argparse.Namespace) -> None:
    if not args.id.upper().startswith("TEST-"):
        raise SystemExit(f"Refusing to create {args.id!r}: --id must start with 'TEST-' (safety guard, see --help).")

    preset = dict(_STATE_PRESETS[args.state])
    if args.fault_type:
        preset["fault_type"] = args.fault_type

    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    record = EquipmentRecord(
        equipment_id=args.id,
        equipment_code=args.code or _default_code(args.id),
        name=args.name or f"[TEST] {args.id}",
        device_type=args.device_type,
        status=preset["status"],
        network_status=preset["network_status"],
        fault_type=preset["fault_type"],
        material_shortage_status=preset["material_shortage_status"],
        observed_at=datetime.now(timezone.utc),
    )

    state_manager = StateManager(HealthEngine())
    with db_base.get_session() as session:
        result = state_manager.process_observation(session, record)
        print(f"equipment_id={args.id} health={result.evaluation.state.value} incident_opened={result.incident_opened is not None}")

        if args.notify:
            alert_engine = AlertEngine(settings, NotificationService(settings))
            alert_engine.notify(session, result)
            if result.incident_opened:
                print(f"notification sent={result.incident_opened.notification_sent}")
            elif result.incident_escalated:
                print(f"escalation notification sent={result.incident_escalated.notification_sent}")
            else:
                print("No new incident/escalation on this observation -- nothing to notify "
                      "(this is correct if the machine was already in this same fault state).")


def remove(args: argparse.Namespace) -> None:
    if not args.id.upper().startswith("TEST-"):
        raise SystemExit(f"Refusing to touch {args.id!r}: --id must start with 'TEST-' (safety guard).")

    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()  # no-op if it already exists; avoids "no such table" against a brand-new DB file

    with db_base.get_session() as session:
        equipment = session.query(Equipment).filter(Equipment.external_id == args.id).one_or_none()
        if equipment is None:
            print(f"{args.id}: not found -- nothing to remove.")
            return
        session.delete(equipment)
        print(f"Removed {args.id} ({equipment.name!r}) and its state/history/incidents.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Add a TEST-* equipment row, or transition an existing one to a new state")
    p_add.add_argument("--id", required=True, help="Equipment ID -- MUST start with 'TEST-'")
    p_add.add_argument("--state", required=True, choices=sorted(_STATE_PRESETS), help="Target health state")
    p_add.add_argument("--name", help="Display name (default: '[TEST] <id>')")
    p_add.add_argument("--code", help="Equipment code (default: derived from --id)")
    p_add.add_argument("--device-type", default="REFRESHA", help="Default: REFRESHA")
    p_add.add_argument("--fault-type", help="Override the raw fault text shown in the alert (state=malfunction only)")
    p_add.add_argument("--notify", action="store_true", help="Also send the real alert email if this is a new fault/escalation")
    p_add.set_defaults(func=add)

    p_remove = sub.add_parser("remove", help="Delete a TEST-* equipment row and all its history/incidents")
    p_remove.add_argument("--id", required=True, help="Equipment ID to remove -- MUST start with 'TEST-'")
    p_remove.set_defaults(func=remove)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
