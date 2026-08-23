# Equipment Health Monitoring & Alerting

Session-aware Playwright monitoring layer for a third-party equipment
management application (Equipment Management → Device Information), built
around the principle that **authentication must be reused, not repeated** —
see [`monitoring/session_manager.py`](monitoring/session_manager.py).

## Current scope

| Phase | Status |
|---|---|
| 1 — Target application discovery | **Done** — confirmed live against the real site on 2026-08-23. See [`docs/target_application_integration_spec.md`](docs/target_application_integration_spec.md). |
| 2 — Monitoring proof of concept | **Done** — [TC-001](tests/tc_001_target_connection/description.md) passes end to end: session reuse (no re-login on repeat runs), real equipment extraction via the target's own JSON API, 6/6 records normalized. |
| 3 — Database + state engine | **Implemented, partially verified.** `HealthEngine` + `StateManager` (incident open/escalate/resolve, dedup) are covered by 21 passing unit tests (`tests/test_health_engine.py`, `tests/test_state_manager.py`) against SQLite. **Not yet run against a real Postgres** — this dev machine has neither Docker nor a native Postgres install. Run `docker compose up -d && python -m db.init_db && python -m monitoring.worker` once Postgres is available to close that gap. |
| 4 — Alerting | Not started (`services/state_manager.py` already reports opened/escalated/resolved incidents; `monitoring/worker.py` logs what *would* be sent — no `AlertEngine`/notifications yet) |
| 5 — Web dashboard | Not started |
| 6 — Production hardening | Not started |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt   # or requirements.txt for runtime-only
python -m playwright install chromium
copy .env.example .env          # then fill in TARGET_USERNAME / TARGET_PASSWORD
```

`.env` is git-ignored. Never commit real credentials — see the warning in
`.env.example` itself.

### Database

```bash
docker compose up -d      # starts Postgres using .env's DB_* values
python -m db.init_db      # creates tables (idempotent)
```

## Running it

```bash
python -m monitoring.worker
```

The current, real entry point: one full poll cycle against the live
target — authenticate-if-needed, extract equipment via the target's JSON
API, evaluate health, persist snapshot/current-state, open or resolve
fault incidents — then exits. Add `--loop` to repeat forever on
`POLL_INTERVAL_SECONDS`, reusing the same persistent browser session
(requirement #12). First run opens a **visible** browser and waits for a
human to complete the CAPTCHA (`HEADLESS=false`); every run after that
reuses the session saved at `data/storage_state/session.json`
(git-ignored) with **no login at all**.

Any technical failure (target unreachable, extraction error, session
expiring mid-run) is classified into a `MonitoringState`
(`AUTHENTICATION_REQUIRED` / `TARGET_UNAVAILABLE` / `EXTRACTION_ERROR` /
`MONITORING_ERROR`), recorded on that cycle's `MonitoringRun` row, and
never turned into a fake equipment health value. See
[`monitoring/models.py`](monitoring/models.py).

`python -m tests.tc_001_target_connection.run` and
`python -m monitoring.poc_runner` still work as lighter-weight
connectivity-only checks (no DB) — see their own docs.

### Tests

```bash
pytest tests/test_health_engine.py tests/test_state_manager.py -v   # unit tests, no live site/DB needed
python -m tests.tc_001_target_connection.run                        # live smoke test against the real target
```

### Re-running discovery from scratch

`discovery/inspect.py` is the original from-scratch discovery tool (opens
the login page cold and lets you navigate manually) — useful if the site
changes enough that the confirmed integration in
`docs/target_application_integration_spec.md` stops working:

```bash
python -m discovery.inspect
```

## Architecture (current pieces)

```
discovery/inspect.py        Phase 1: one-off manual DOM/network capture (from-scratch)
docs/target_application_integration_spec.md
                             Phase 1 deliverable: confirmed target app structure

monitoring/
  config.py                 env-driven settings, incl. DB connection (no hardcoded secrets)
  models.py                 MonitoringState, EquipmentRecord, exceptions
  selectors.py               target URLs/API paths/field maps — the file to
                             edit first if the target site changes
  browser_manager.py        owns the ONE persistent browser/context/page
  session_manager.py        session-aware auth: reuse valid session, only
                             authenticate when invalid/expired (core rule)
  target_client.py          all target-site interaction (nav, JSON API
                             extraction, DOM fallback)
  equipment_extractor.py    canonical rows -> validated EquipmentRecord
  validator.py               "zero records" is a validation failure, not a
                             fleet-wide OFFLINE state (requirement #28)
  worker.py                  Phase 3 real poll-cycle entry point (DB-backed)
  poc_runner.py              Phase 2 connectivity-only demo (no DB)

db/
  models.py                  equipment / equipment_current_state /
                             equipment_snapshot / fault_incident / users /
                             alert_subscription / monitoring_run (spec §16)
  base.py                    engine/session (sync SQLAlchemy — see its
                             docstring for why, given the async worker)
  init_db.py                 `python -m db.init_db` — creates tables

config/
  health_rules.yaml           configurable health-evaluation rules (§7) —
                             edit this, not health_engine.py, to add values

services/
  health_engine.py            raw target fields -> canonical HealthState
                             (HEALTHY/WARNING/MALFUNCTION/OFFLINE/UNKNOWN)
  state_manager.py            the alerting-correctness core: detects a
                             malfunction exactly once, tracks it as one
                             incident until recovery (§8/§9)

tests/
  conftest.py                 shared fixtures (in-memory SQLite + record factory)
  test_health_engine.py       unit tests for the rule table
  test_state_manager.py       unit tests for incident dedup/escalation/
                             resolution — reproduces the spec's own
                             "one alert, not four" worked example
  tc_001_target_connection/   numbered live test case: connect, authenticate
                             (reuse-first), extract, report malfunctions
```

Deliberately **not yet built**: `AlertEngine`/notifications, FastAPI, the
Next.js dashboard, Alembic migrations (using `Base.metadata.create_all()`
for now — fine for one evolving dev schema, revisit before a real
production cutover) — Phase 4–6.

Two things worth knowing about the current health rules:
their real-world fault vocabulary is still unconfirmed (no device in this
account has reported a non-"Normal" `fault_type`/`material_shortage_status`
yet — see "still unconfirmed" in the integration spec), and the API's
`this_fault` field (rich per-component fault detail) isn't wired into
`fault_type` yet, only preserved in `EquipmentRecord.raw`/`FaultIncident`'s
audit trail.

## Next steps (not yet implemented)

- Finish Phase 3: run `db/init_db.py` + `monitoring/worker.py --loop`
  against a real Postgres once Docker/Postgres is available here.
- Phase 4: `AlertEngine` + `NotificationService` (email first) —
  `StateManager` already hands back exactly the opened/escalated/resolved
  incidents an alert engine needs; `FaultIncident.notification_sent`
  exists and is currently always `False`.
- Phase 5: FastAPI backend + Next.js dashboard (fleet overview, equipment
  detail, active faults, monitoring status).
- Phase 6: hardening (Alembic migrations, structured logging, RBAC,
  Docker image for the worker itself, secrets manager, backups).
