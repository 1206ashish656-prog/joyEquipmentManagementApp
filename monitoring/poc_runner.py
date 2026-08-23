"""
Phase 2 proof of concept (project spec section "Phase 2 — Monitoring Proof
of Concept"):

    Playwright -> Authenticate -> Maintain session -> Equipment Management
    -> Device Information -> Extract equipment -> Print normalized records

Goal: demonstrate that ONE persistent browser session can retrieve
equipment data repeatedly without logging in again each cycle. No
database, API, or alerting yet — that's Phase 3+.

Run:
    python -m monitoring.poc_runner
"""
from __future__ import annotations

import asyncio
import logging

from .browser_manager import BrowserManager
from .config import load_settings
from .equipment_extractor import EquipmentExtractor
from .models import EquipmentRecord, MonitoringError
from .session_manager import SessionManager
from .target_client import TargetApplicationClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("poc_runner")


def _print_record(rec: EquipmentRecord) -> None:
    print(
        f"  ID={rec.equipment_id:<8} Code={rec.equipment_code:<20} "
        f"Name={rec.name:<12} Type={rec.device_type:<12} "
        f"Status={rec.status:<10} Network={rec.network_status:<10} "
        f"Fault={rec.fault_type:<14} Material={rec.material_shortage_status:<10} "
        f"ObservedAt={rec.observed_at.isoformat()}"
    )


async def run_poc() -> None:
    settings = load_settings()

    if not settings.target_login_url:
        raise SystemExit("TARGET_LOGIN_URL is not set. Copy .env.example to .env and fill it in.")

    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    page = await browser_manager.start()
    client = TargetApplicationClient(page, settings)
    session = SessionManager(browser_manager, client)
    extractor = EquipmentExtractor()

    try:
        for cycle in range(1, settings.poc_iterations + 1):
            print(f"\n===== Poll cycle {cycle}/{settings.poc_iterations} =====")
            try:
                await session.ensure_authenticated()
                await client.open_device_information()

                raw_rows = await client.get_equipment_data()
                raw_rows = extractor.extract(raw_rows)
                records = extractor.normalize(raw_rows)
                extractor.validate(records, min_expected=1)

                print(f"Retrieved {len(records)} equipment record(s):")
                for rec in records:
                    _print_record(rec)

            except MonitoringError as e:
                # Requirement #28: a monitoring/technical failure is NEVER
                # turned into an equipment health state. It's surfaced as
                # its own monitoring state instead.
                logger.error("Monitoring state = %s: %s", e.state.value, e)

            if cycle < settings.poc_iterations:
                print(f"Waiting {settings.poll_interval_seconds}s until next poll "
                      f"(session stays open — no re-login)...")
                await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await browser_manager.close()


if __name__ == "__main__":
    asyncio.run(run_poc())
