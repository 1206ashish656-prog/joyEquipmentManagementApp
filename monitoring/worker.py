"""
Phase 3 monitoring worker: the real poll-cycle pipeline from spec section
11, now persisting to Postgres (equipment master, current state,
snapshots, fault incidents) via services/state_manager.py, plus a
MonitoringRun audit row per cycle (spec section 16).

Still does NOT send notifications (Phase 4, services/alert_engine.py) —
it prints what would be alerted and leaves `notification_sent=False` on
any opened/escalated incident for the future AlertEngine to consume.

Run:
    python -m monitoring.worker            # single poll cycle, then exit
    python -m monitoring.worker --loop     # repeat forever, using the
                                            # SAME persistent browser
                                            # session (requirement #12)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from db import base as db_base
from db.models import MonitoringRun, MonitoringRunStatus
from monitoring.browser_manager import BrowserManager
from monitoring.config import load_settings
from monitoring.equipment_extractor import EquipmentExtractor
from monitoring.models import MonitoringError
from monitoring.session_manager import SessionManager
from monitoring.target_client import TargetApplicationClient
from services.health_engine import HealthEngine
from services.state_manager import ObservationResult, StateManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


def _log_result(equipment_name: str, equipment_id: str, result: ObservationResult) -> None:
    if result.incident_opened:
        logger.warning(
            "NEW %s: equipment=%s(%s) fault_type=%r severity=%s "
            "[Phase 4 will notify subscribers — not sent yet]",
            result.evaluation.state.value, equipment_name, equipment_id,
            result.incident_opened.fault_type, result.incident_opened.severity,
        )
    elif result.incident_escalated:
        logger.warning(
            "ESCALATED equipment=%s(%s) -> %s fault_type=%r",
            equipment_name, equipment_id, result.evaluation.state.value, result.incident_escalated.fault_type,
        )
    elif result.incident_resolved:
        downtime = result.incident_resolved.resolved_at - result.incident_resolved.started_at
        logger.info(
            "RECOVERED equipment=%s(%s) downtime=%s [Phase 4 will send recovery notice — not sent yet]",
            equipment_name, equipment_id, downtime,
        )
    elif result.state_changed:
        logger.info("state changed: equipment=%s(%s) -> %s", equipment_name, equipment_id, result.evaluation.state.value)
    else:
        logger.debug("no change: equipment=%s(%s) = %s", equipment_name, equipment_id, result.evaluation.state.value)


async def run_once(
    session: SessionManager,
    client: TargetApplicationClient,
    extractor: EquipmentExtractor,
    state_manager: StateManager,
) -> MonitoringRun:
    """One full poll cycle. DB writes happen synchronously in this
    coroutine (not offloaded to a thread) — at this scale (one session,
    a handful to low hundreds of equipment rows per poll, one poll a
    minute) the write volume is small enough that briefly blocking the
    event loop is an acceptable, simpler tradeoff than a second
    threading/async-session story."""
    with db_base.get_session() as db_session:
        run = MonitoringRun(status=MonitoringRunStatus.RUNNING)
        db_session.add(run)
        db_session.flush()

        try:
            await session.ensure_authenticated()
            await client.open_device_information()

            raw_rows = await client.get_equipment_data()
            raw_rows = extractor.extract(raw_rows)
            records = extractor.normalize(raw_rows)
            extractor.validate(records, min_expected=1)

            run.records_found = len(records)
            processed = 0
            for record in records:
                result = state_manager.process_observation(db_session, record)
                _log_result(record.name, record.equipment_id, result)
                processed += 1

            run.records_processed = processed
            run.status = (
                MonitoringRunStatus.SUCCESS if processed == run.records_found else MonitoringRunStatus.PARTIAL
            )

        except MonitoringError as e:
            # Requirement #28/#25: a monitoring/technical failure is
            # recorded on the run, never turned into equipment health.
            run.status = MonitoringRunStatus.FAILED
            run.error_message = f"{e.state.value}: {e}"
            logger.error("Monitoring run failed: %s", run.error_message)
        except Exception as e:  # noqa: BLE001 - top-level guard, must not crash the loop
            run.status = MonitoringRunStatus.FAILED
            run.error_message = f"UNEXPECTED: {e!r}"
            logger.exception("Unexpected monitoring run failure")
        finally:
            run.completed_at = datetime.now(timezone.utc)

        return run


async def run_forever(poll_interval_seconds: int) -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    page = await browser_manager.start()
    client = TargetApplicationClient(page, settings)
    session = SessionManager(browser_manager, client)
    extractor = EquipmentExtractor()
    state_manager = StateManager(HealthEngine())

    logger.info("Monitoring worker started. Polling every %ss.", poll_interval_seconds)
    try:
        while True:
            run = await run_once(session, client, extractor, state_manager)
            logger.info(
                "Poll cycle complete: status=%s found=%s processed=%s",
                run.status.value, run.records_found, run.records_processed,
            )
            await asyncio.sleep(poll_interval_seconds)
    finally:
        await browser_manager.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Equipment monitoring worker")
    parser.add_argument("--loop", action="store_true", help="Poll repeatedly instead of once")
    args = parser.parse_args()

    settings = load_settings()

    if args.loop:
        await run_forever(settings.poll_interval_seconds)
        return

    db_base.init_engine(settings)
    db_base.create_all()

    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    page = await browser_manager.start()
    client = TargetApplicationClient(page, settings)
    session = SessionManager(browser_manager, client)
    extractor = EquipmentExtractor()
    state_manager = StateManager(HealthEngine())

    try:
        run = await run_once(session, client, extractor, state_manager)
        print(f"\nRun status: {run.status.value}")
        print(f"Records found/processed: {run.records_found}/{run.records_processed}")
        if run.error_message:
            print(f"Error: {run.error_message}")
    finally:
        await browser_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
