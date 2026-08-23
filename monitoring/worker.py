"""
Monitoring worker: the real poll-cycle pipeline from spec section 11.
Persists to Postgres (equipment master, current state, snapshots, fault
incidents) via services/state_manager.py, records a MonitoringRun audit
row per cycle (spec section 16), and now (Phase 4) actually notifies
subscribers via services/alert_engine.py on new/escalated/resolved
incidents.

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
from dataclasses import dataclass
from datetime import datetime, timezone

from db import base as db_base
from db.models import MonitoringRun, MonitoringRunStatus
from monitoring.browser_manager import BrowserManager
from monitoring.config import Settings, load_settings
from monitoring.equipment_extractor import EquipmentExtractor
from monitoring.models import MonitoringError
from monitoring.session_manager import SessionManager
from monitoring.target_client import TargetApplicationClient
from services.alert_engine import AlertEngine
from services.health_engine import HealthEngine
from services.notification_service import NotificationService
from services.state_manager import ObservationResult, StateManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")


@dataclass
class WorkerContext:
    settings: Settings
    browser_manager: BrowserManager
    session: SessionManager
    client: TargetApplicationClient
    extractor: EquipmentExtractor
    state_manager: StateManager
    alert_engine: AlertEngine


async def build_context(settings: Settings | None = None) -> WorkerContext:
    settings = settings or load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    page = await browser_manager.start()
    client = TargetApplicationClient(page, settings)
    session = SessionManager(browser_manager, client)

    return WorkerContext(
        settings=settings,
        browser_manager=browser_manager,
        session=session,
        client=client,
        extractor=EquipmentExtractor(),
        state_manager=StateManager(HealthEngine()),
        alert_engine=AlertEngine(settings, NotificationService(settings)),
    )


def _log_result(equipment_name: str, equipment_id: str, result: ObservationResult) -> None:
    if result.incident_opened:
        logger.warning(
            "NEW %s: equipment=%s(%s) fault_type=%r severity=%s",
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
        logger.info("RECOVERED equipment=%s(%s) downtime=%s", equipment_name, equipment_id, downtime)
    elif result.state_changed:
        logger.info("state changed: equipment=%s(%s) -> %s", equipment_name, equipment_id, result.evaluation.state.value)
    else:
        logger.debug("no change: equipment=%s(%s) = %s", equipment_name, equipment_id, result.evaluation.state.value)


async def run_once(ctx: WorkerContext) -> MonitoringRun:
    """One full poll cycle. DB writes (and alert sends) happen
    synchronously in this coroutine (not offloaded to a thread) — at this
    scale (one session, a handful to low hundreds of equipment rows per
    poll, one poll a minute) the volume is small enough that briefly
    blocking the event loop is an acceptable, simpler tradeoff than a
    second threading/async-session story."""
    with db_base.get_session() as db_session:
        run = MonitoringRun(status=MonitoringRunStatus.RUNNING)
        db_session.add(run)
        db_session.flush()

        try:
            await ctx.session.ensure_authenticated()
            await ctx.client.open_device_information()

            raw_rows = await ctx.client.get_equipment_data()
            raw_rows = ctx.extractor.extract(raw_rows)
            records = ctx.extractor.normalize(raw_rows)
            ctx.extractor.validate(records, min_expected=1)

            run.records_found = len(records)
            processed = 0
            for record in records:
                result = ctx.state_manager.process_observation(db_session, record)
                _log_result(record.name, record.equipment_id, result)
                if result.incident_opened or result.incident_escalated or result.incident_resolved:
                    ctx.alert_engine.notify(db_session, result)
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


async def run_forever(ctx: WorkerContext) -> None:
    logger.info("Monitoring worker started. Polling every %ss.", ctx.settings.poll_interval_seconds)
    while True:
        run = await run_once(ctx)
        logger.info(
            "Poll cycle complete: status=%s found=%s processed=%s",
            run.status.value, run.records_found, run.records_processed,
        )
        await asyncio.sleep(ctx.settings.poll_interval_seconds)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Equipment monitoring worker")
    parser.add_argument("--loop", action="store_true", help="Poll repeatedly instead of once")
    args = parser.parse_args()

    ctx = await build_context()
    try:
        if args.loop:
            await run_forever(ctx)
        else:
            run = await run_once(ctx)
            print(f"\nRun status: {run.status.value}")
            print(f"Records found/processed: {run.records_found}/{run.records_processed}")
            if run.error_message:
                print(f"Error: {run.error_message}")
    finally:
        await ctx.browser_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
