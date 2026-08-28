"""
Monitoring worker: the real poll-cycle pipeline from spec section 11.
Persists to Postgres (equipment master, current state, snapshots, fault
incidents) via services/state_manager.py, records a MonitoringRun audit
row per cycle (spec section 16), and notifies subscribers via
services/alert_engine.py on new/escalated/resolved incidents.

Steady-state polling uses LightweightTargetClient (plain HTTP, no
browser/Chromium at all — see lightweight_client.py's docstring). A real
Playwright browser is launched ONLY transiently, when the session turns
out to be invalid/expired, to run the human-in-the-loop CAPTCHA login —
then it's closed immediately. This means a running `--loop` worker does
NOT keep a visible (or hidden) Chrome process open/navigating every poll;
Chrome appears only for the rare re-auth event.

Run:
    python -m monitoring.worker            # single poll cycle, then exit
    python -m monitoring.worker --loop     # repeat forever
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from db import base as db_base
from db.models import FaultIncident, FaultLogEntry, MonitoringRun, MonitoringRunStatus
from monitoring.browser_manager import BrowserManager
from monitoring.config import Settings, load_settings
from monitoring.equipment_extractor import EquipmentExtractor
from monitoring.lightweight_client import LightweightTargetClient
from monitoring.models import AuthenticationRequiredError, MonitoringError
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
    lightweight: LightweightTargetClient
    extractor: EquipmentExtractor
    state_manager: StateManager
    alert_engine: AlertEngine


def build_context(settings: Settings | None = None) -> WorkerContext:
    """No browser is launched here — steady-state polling never needs
    one. See _reauthenticate() for the only place a browser gets started."""
    settings = settings or load_settings()
    db_base.init_engine(settings)
    db_base.create_all()

    return WorkerContext(
        settings=settings,
        lightweight=LightweightTargetClient(settings),
        extractor=EquipmentExtractor(),
        state_manager=StateManager(HealthEngine()),
        alert_engine=AlertEngine(settings, NotificationService(settings)),
    )


async def _reauthenticate(settings: Settings) -> None:
    """The ONLY place a Playwright browser gets launched. Runs the
    existing, fully-tested SessionManager/TargetApplicationClient login
    flow (human completes the CAPTCHA in a visible window if HEADLESS is
    false), persists the resulting session to disk, then closes the
    browser immediately — it does not stay open for polling."""
    logger.info("Session invalid/missing — launching a browser for one-time re-authentication")
    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    try:
        page = await browser_manager.start()
        client = TargetApplicationClient(page, settings)
        session = SessionManager(browser_manager, client)
        await session.ensure_authenticated()
    finally:
        await browser_manager.close()


async def _attach_fault_log_detail(
    lightweight: LightweightTargetClient,
    db_session,
    target_device_id: str,
    incident: FaultIncident,
) -> None:
    """Best-effort enrichment: fetches the target's own per-component
    Fault Information rows (device/device_fault_log) for this device and
    attaches them to the just-opened/escalated incident, so the alert
    email and /faults/<id> page can show real detail (e.g. "Electronic
    scale Malfunction") instead of only the coarse fault_type text.

    Deliberately never lets a failure here propagate -- this runs AFTER
    the incident itself is already committed to the DB, so a target
    hiccup at this exact moment must degrade to "no extra detail this
    time", never break incident detection or suppress the alert."""
    try:
        rows = await lightweight.get_active_fault_log(target_device_id)
    except Exception:  # noqa: BLE001 - enrichment only, must never break alerting
        logger.warning("Could not fetch fault log detail for device %s", target_device_id, exc_info=True)
        return

    for row in rows:
        db_session.add(
            FaultLogEntry(
                incident_id=incident.id,
                target_log_id=row["target_log_id"],
                component_code=row["component_code"],
                component_description=row["component_description"],
                is_stop=row["is_stop"],
                is_clean=row["is_clean"],
                occurred_at=row["occurred_at"],
                cleared_at=row["cleared_at"],
            )
        )
    if rows:
        db_session.flush()


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
    """One full poll cycle: session check -> (rare) browser re-auth ->
    lightweight HTTP fetch -> validate -> evaluate -> persist -> alert."""
    with db_base.get_session() as db_session:
        run = MonitoringRun(status=MonitoringRunStatus.RUNNING)
        db_session.add(run)
        db_session.flush()

        try:
            if not await ctx.lightweight.is_session_valid():
                await _reauthenticate(ctx.settings)
                ctx.lightweight.reload_cookies()
                if not await ctx.lightweight.is_session_valid():
                    raise AuthenticationRequiredError(
                        "Still redirected to login after browser re-authentication attempt."
                    )
            else:
                logger.info("Existing session is valid (lightweight check, no browser) — reusing it.")

            raw_rows = await ctx.lightweight.get_equipment_data()
            raw_rows = ctx.extractor.extract(raw_rows)
            records = ctx.extractor.normalize(raw_rows)
            ctx.extractor.validate(records, min_expected=1)

            run.records_found = len(records)
            processed = 0
            for record in records:
                result = ctx.state_manager.process_observation(db_session, record)
                _log_result(record.name, record.equipment_id, result)
                incident_for_detail = result.incident_opened or result.incident_escalated
                if incident_for_detail is not None:
                    await _attach_fault_log_detail(ctx.lightweight, db_session, record.equipment_id, incident_for_detail)
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
    logger.info(
        "Monitoring worker started (lightweight/no-browser steady state). Polling every %ss.",
        ctx.settings.poll_interval_seconds,
    )
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

    ctx = build_context()
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
        await ctx.lightweight.close()


if __name__ == "__main__":
    asyncio.run(main())
