"""
Fault log history backfill: for each tracked piece of equipment, fetches
the target application's COMPLETE historical Fault Information log
(device/device_fault_log — no date-range filtering; the target itself
only exposes a device_id filter) and stores every row into
FaultLogHistory (db/models.py) — the sole source for the Senior
Management Report's "Downtime per Machine" section
(services/management_report.py), chosen specifically because this has
real historical depth, unlike FaultIncident which only has data from
whenever this app's own polling started running.

Safe to re-run anytime (idempotent): FaultLogHistory's
(equipment_id, target_log_id) unique constraint means an already-stored
row is simply skipped, never duplicated — same "re-run whenever, only
new rows get added" shape as orders/backfill.py, just without that
module's date-range/day-by-day structure (there's no day to already have
"succeeded" here; each individual target_log_id is its own dedup key).

Run:
    python -m monitoring.fault_log_backfill
    python -m monitoring.fault_log_backfill --equipment-id 205   # one machine only (its external_id)
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from db import base as db_base
from db.models import Equipment, FaultLogHistory
from monitoring.config import load_settings
from monitoring.lightweight_client import LightweightTargetClient
from monitoring.models import MonitoringError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("monitoring.fault_log_backfill")


async def backfill_one_equipment(client: LightweightTargetClient, equipment: Equipment) -> int:
    """Returns how many NEW rows were stored (0 on a repeat run once
    everything's already been backfilled)."""
    rows = await client.get_fault_log_history(equipment.external_id)

    with db_base.get_session() as db:
        existing_ids = {
            target_log_id
            for (target_log_id,) in db.execute(
                select(FaultLogHistory.target_log_id).where(FaultLogHistory.equipment_id == equipment.id)
            )
        }
        created = 0
        for row in rows:
            if row["target_log_id"] in existing_ids:
                continue
            db.add(FaultLogHistory(
                equipment_id=equipment.id,
                target_log_id=row["target_log_id"],
                component_code=row["component_code"],
                component_description=row["component_description"],
                is_stop=row["is_stop"],
                is_clean=row["is_clean"],
                occurred_at=row["occurred_at"],
                cleared_at=row["cleared_at"],
            ))
            created += 1

    return created


async def run_backfill(equipment_external_id: str | None = None) -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    client = LightweightTargetClient(settings)
    try:
        if not await client.is_session_valid():
            raise SystemExit(
                "Target session invalid/expired — run `python -m monitoring.worker` once "
                "(or the combined worker) to re-authenticate first, then retry this backfill."
            )

        with db_base.get_session() as db:
            query = select(Equipment)
            if equipment_external_id:
                query = query.where(Equipment.external_id == equipment_external_id)
            equipment_list = db.execute(query).scalars().all()

        if not equipment_list:
            logger.warning("No matching equipment found — nothing to backfill.")
            return

        total_created = 0
        for equipment in equipment_list:
            try:
                created = await backfill_one_equipment(client, equipment)
            except MonitoringError as e:
                # One machine's fetch failing must not abandon the rest —
                # same "a single failure never corrupts/blocks the whole
                # run" principle as orders/backfill.py.
                logger.error("%s (device %s): fetch failed (%s) — skipping", equipment.name, equipment.external_id, e)
                continue
            logger.info("%s (device %s): %d new fault log row(s) stored", equipment.name, equipment.external_id, created)
            total_created += created

        logger.info("Backfill complete: %d new row(s) total across %d machine(s)", total_created, len(equipment_list))
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--equipment-id", help="Backfill only this one machine (its external_id / target device_id) — default: all",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(args.equipment_id))


if __name__ == "__main__":
    main()
