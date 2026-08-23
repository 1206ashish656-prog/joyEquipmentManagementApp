"""
TC-001 — Target connection & equipment malfunction extraction.
See description.md in this folder for the full test case.

This is a single-shot smoke test (one poll cycle), not the Phase 2 POC
loop (monitoring/poc_runner.py polls repeatedly). It reuses the exact same
production-shaped components (BrowserManager, SessionManager,
TargetApplicationClient, EquipmentExtractor) so a pass here means the real
pipeline works, not just a mock.

The malfunction heuristic below is TEST-ONLY scaffolding to answer "is
anything reporting a fault right now" for this smoke test — it is
deliberately NOT the real HealthEngine (that's Phase 3, and needs
confirmed real-world fault_type/network_status values first).

Run:
    python -m tests.tc_001_target_connection.run
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from monitoring.browser_manager import BrowserManager
from monitoring.config import load_settings
from monitoring.equipment_extractor import EquipmentExtractor
from monitoring.models import EquipmentRecord, MonitoringError
from monitoring.session_manager import SessionManager
from monitoring.target_client import TargetApplicationClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tc_001")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Test-only heuristic — NOT the production health engine.
_NO_FAULT_VALUES = {"normal", "none", "no fault", "-", ""}


def _looks_like_malfunction(rec: EquipmentRecord) -> bool:
    fault = (rec.fault_type or "").strip().lower()
    network = (rec.network_status or "").strip().lower()
    return fault not in _NO_FAULT_VALUES or network == "offline"


def _record_to_json(rec: EquipmentRecord) -> dict:
    d = asdict(rec)
    d["observed_at"] = rec.observed_at.isoformat()
    return d


async def main() -> int:
    settings = load_settings()
    if not settings.target_login_url or not settings.has_credentials:
        print("FAIL: TARGET_LOGIN_URL / TARGET_USERNAME / TARGET_PASSWORD not set in .env")
        return 1

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    outcome = "FAIL"
    error_message = None
    records: list[EquipmentRecord] = []

    browser_manager = BrowserManager(settings.storage_state_path, headless=settings.headless)
    try:
        page = await browser_manager.start()
        client = TargetApplicationClient(page, settings)
        session = SessionManager(browser_manager, client)
        extractor = EquipmentExtractor()

        print("=" * 70)
        print("TC-001: target connection & equipment malfunction extraction")
        print("=" * 70)

        await session.ensure_authenticated()
        await client.open_device_information()

        raw_rows = await client.get_equipment_data()
        raw_rows = extractor.extract(raw_rows)
        records = extractor.normalize(raw_rows)
        extractor.validate(records, min_expected=1)

        outcome = "PASS"
    except MonitoringError as e:
        error_message = f"{e.state.value}: {e}"
        logger.error("Monitoring state = %s", error_message)
    except Exception as e:  # noqa: BLE001 - top-level test harness, want to record anything
        error_message = f"UNEXPECTED: {e!r}"
        logger.exception("Unexpected failure")
    finally:
        await browser_manager.close()

    malfunctioning = [r for r in records if _looks_like_malfunction(r)]

    print(f"\nOutcome: {outcome}")
    if error_message:
        print(f"Error: {error_message}")
    print(f"Total equipment records retrieved: {len(records)}")
    print(f"Records that look like a malfunction (test heuristic, not the real health engine): "
          f"{len(malfunctioning)}")
    for rec in malfunctioning:
        print(
            f"  MALFUNCTION? ID={rec.equipment_id} Name={rec.name} "
            f"Fault={rec.fault_type!r} Network={rec.network_status!r} "
            f"Material={rec.material_shortage_status!r}"
        )

    result_path = RESULTS_DIR / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    result_payload = {
        "test": "TC-001",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "error": error_message,
        "total_records": len(records),
        "malfunctioning_count": len(malfunctioning),
        "malfunctioning_ids": [r.equipment_id for r in malfunctioning],
        "records": [_record_to_json(r) for r in records],
    }
    result_path.write_text(json.dumps(result_payload, indent=2), encoding="utf-8")
    print(f"\nFull result written to: {result_path}")

    return 0 if outcome == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
