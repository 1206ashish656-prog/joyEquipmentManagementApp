"""
Combined process for cloud deployment: runs monitoring.worker's
equipment-polling loop and orders.realtime_worker's order-refresh loop
CONCURRENTLY in one process, so they share one container filesystem —
and therefore one Playwright session file — instead of needing session
state duplicated across two separately-deployed services.

Why this exists: only monitoring.worker ever re-authenticates (the
Playwright flow in worker.py's _reauthenticate()), and keeps its own
httpx client's cookies fresh afterward. Hosting platforms (Railway,
Render, ...) attach a persistent volume to exactly ONE service each —
running the two loops as two separate deployed services would leave
orders.realtime_worker with no way to ever see a fresh session after
the first one, since it has no volume of its own to read from. Putting
both loops in one process/service/volume sidesteps that entirely.
(orders/client.py's OrdersClient.reload_cookies(), called once per
cycle by orders/realtime_worker.py's run_cycle(), is the other half of
this fix — it's what actually notices the file changed; being in the
same process only makes the file visible in the first place.)

Both loops are used completely unmodified — this is a thin
asyncio.gather() wrapper, no duplicated logic. Each also still works
perfectly well run standalone (`python -m monitoring.worker --loop` /
`python -m orders.realtime_worker --loop`) for local dev, exactly as
before this module existed.

Run:
    python -m monitoring.combined_worker --loop
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from monitoring.worker import build_context, run_forever
from orders.realtime_worker import DEFAULT_INTERVAL_SECONDS as ORDERS_DEFAULT_INTERVAL_SECONDS
from orders.realtime_worker import run_loop as run_orders_loop

logger = logging.getLogger("monitoring.combined_worker")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the equipment-monitoring loop and the orders-refresh loop together in one process"
    )
    parser.add_argument("--loop", action="store_true", required=True, help="Required — this process only makes sense running forever")
    parser.add_argument(
        "--orders-interval", type=int, default=ORDERS_DEFAULT_INTERVAL_SECONDS,
        help=f"Seconds between orders refreshes (default {ORDERS_DEFAULT_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()

    ctx = build_context()
    logger.info("Combined worker starting: monitoring loop + orders loop in one process.")
    try:
        # Either loop raising is treated as fatal for the whole process
        # (not caught/suppressed here) — same "never silently keep only
        # half working" principle both loops already follow internally
        # for their own per-cycle failures.
        await asyncio.gather(
            run_forever(ctx),
            run_orders_loop(args.orders_interval),
        )
    finally:
        await ctx.lightweight.close()


if __name__ == "__main__":
    asyncio.run(main())
