"""
Background processing for Senior Management Report jobs (db/models.py's
ReportJob). Per explicit request -- "an isolated report generation
process" -- the actual computation (services/management_report.py) and
PDF rendering (services/report_pdf.py's render_management_report_pdf,
a real headless-Chromium launch) run ONLY here, in the standalone
worker process (wired into monitoring/combined_worker.py as a third
concurrent loop), never inline in a web request. backend/api/reports.py
only ever creates a PENDING row and later reads the finished one back.

One job's failure (a bug, an equipment_id that no longer exists,
whatever) is recorded on that job (status=FAILED, error_message) and
must NEVER crash this loop or the process it shares with equipment
monitoring/orders sync -- same "top-level guard, must not crash the
loop" principle monitoring/worker.py's run_once() already follows for
its own per-cycle failures.

Run standalone for local dev, exactly like the other two loops:
    python -m services.report_job_worker --loop
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from db import base as db_base
from db.models import ReportJob, ReportJobStatus
from monitoring.config import load_settings
from services.report_pdf import render_management_report_pdf

logger = logging.getLogger("services.report_job_worker")

DEFAULT_INTERVAL_SECONDS = 10  # a report isn't time-critical; short enough to feel responsive after clicking Generate


async def process_pending_jobs() -> int:
    """Processes every currently-PENDING job once, oldest first. Returns
    how many were processed (succeeded or failed -- both count, since
    both are a job reaching a terminal state)."""
    with db_base.get_session() as db:
        pending_ids = db.execute(
            select(ReportJob.id).where(ReportJob.status == ReportJobStatus.PENDING).order_by(ReportJob.created_at)
        ).scalars().all()

    processed = 0
    for job_id in pending_ids:
        with db_base.get_session() as db:
            job = db.get(ReportJob, job_id)
            if job is None or job.status != ReportJobStatus.PENDING:
                continue  # defensive -- only one worker runs this loop, but never assume it
            job.status = ReportJobStatus.RUNNING
            db.flush()

            try:
                pdf_bytes = await render_management_report_pdf(
                    db, start=job.start, end=job.end, equipment_id=job.equipment_id, period=job.period,
                )
            except Exception as e:  # noqa: BLE001 -- one job's failure must never crash the shared worker loop
                logger.exception("Report job %s failed", job_id)
                job.status = ReportJobStatus.FAILED
                job.error_message = f"{type(e).__name__}: {e}"[:2000]
                job.completed_at = datetime.now(timezone.utc)
            else:
                job.pdf_data = pdf_bytes
                job.status = ReportJobStatus.SUCCESS
                job.completed_at = datetime.now(timezone.utc)
                logger.info("Report job %s completed (%d bytes)", job_id, len(pdf_bytes))
        processed += 1

    return processed


async def run_loop(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    settings = load_settings()
    db_base.init_engine(settings)
    db_base.create_all()
    logger.info("Report job worker started. Polling every %ss.", interval_seconds)
    while True:
        try:
            await process_pending_jobs()
        except Exception:  # noqa: BLE001 -- top-level guard, must not crash the loop
            logger.exception("Unexpected error in report job loop")
        await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process pending Senior Management Report jobs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true", help="Run forever, polling every --interval seconds")
    mode.add_argument("--once", action="store_true", help="Process whatever is pending once and exit")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between polls in --loop mode (default {DEFAULT_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        settings = load_settings()
        db_base.init_engine(settings)
        db_base.create_all()
        asyncio.run(process_pending_jobs())
    else:
        asyncio.run(run_loop(args.interval))


if __name__ == "__main__":
    main()
