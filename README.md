# Equipment Health Monitoring & Alerting

Session-aware Playwright monitoring layer for a third-party equipment
management application (Equipment Management → Device Information), built
around the principle that **authentication must be reused, not repeated** —
see [`monitoring/session_manager.py`](monitoring/session_manager.py).

This repo currently implements **Phase 1 (target discovery)** and **Phase 2
(monitoring proof of concept)** only, by design — see "Current scope"
below. Phases 3–6 (database, alerting, dashboard, hardening) are not built
yet.

## Current scope

| Phase | Status |
|---|---|
| 1 — Target application discovery | **Done** — confirmed live against the real site on 2026-08-23. See [`docs/target_application_integration_spec.md`](docs/target_application_integration_spec.md). |
| 2 — Monitoring proof of concept | **Done** — [TC-001](tests/tc_001_target_connection/description.md) passes end to end: session reuse (no re-login on repeat runs), real equipment extraction via the target's own JSON API, 6/6 records normalized. |
| 3 — Database + state engine | Not started |
| 4 — Alerting | Not started |
| 5 — Web dashboard | Not started |
| 6 — Production hardening | Not started |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env          # then fill in TARGET_USERNAME / TARGET_PASSWORD
```

`.env` is git-ignored. Never commit real credentials — see the warning in
`.env.example` itself.

## Running it

```bash
python -m tests.tc_001_target_connection.run
```

This is the current, up-to-date entry point: single poll cycle against the
real target, using the confirmed JSON API extraction path. First run opens
a **visible** browser and waits for a human to complete the CAPTCHA
(`HEADLESS=false`); every run after that reuses the session saved at
`data/storage_state/session.json` (git-ignored) with **no login at all** —
you'll see `"Existing session is valid — reusing it (no login performed)."`
Writes a timestamped result to `tests/tc_001_target_connection/results/`
(git-ignored — contains real equipment data) and prints which equipment
currently looks like it's offline/faulted.

`monitoring/poc_runner.py` does the same thing but as a repeating loop
(`POLL_INTERVAL_SECONDS` / `POC_ITERATIONS` in `.env`) — useful for
watching session reuse hold up across multiple cycles:

```bash
python -m monitoring.poc_runner
```

Any technical failure (target unreachable, extraction error, session
expiring mid-run) is classified into a `MonitoringState`
(`AUTHENTICATION_REQUIRED` / `TARGET_UNAVAILABLE` / `EXTRACTION_ERROR` /
`MONITORING_ERROR`) and printed — it is never turned into a fake equipment
health value. See [`monitoring/models.py`](monitoring/models.py).

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
  config.py                 env-driven settings (no hardcoded secrets)
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
  poc_runner.py              Phase 2 repeating-poll entry point

tests/
  tc_001_target_connection/  numbered test case: connect, authenticate
                             (reuse-first), extract, report malfunctions
```

Deliberately **not yet built**: health engine (HEALTHY/WARNING/MALFUNCTION/
OFFLINE/UNKNOWN), Postgres persistence, incident/state-transition tracking,
alerting/notifications, FastAPI, and the Next.js dashboard — Phase 3–5.
Building the real health-rule mapping table needs at least one observed
non-"Normal" `fault_type`/`material_shortage_status` value, which this
account hasn't produced yet (see "still unconfirmed" in the integration
spec).

## Next steps (not yet implemented)

- Phase 3: Postgres schema (`equipment`, `equipment_current_state`,
  `equipment_snapshot`, `fault_incident`, ...), `HealthEngine`, state
  transition detection. Strong candidate for richer fault detail: the
  API's `this_fault` field (per-component fault flags) — see integration
  spec.
- Phase 4: `AlertEngine` + `NotificationService` (email first), incident
  dedup/cooldown.
- Phase 5: FastAPI backend + Next.js dashboard (fleet overview, equipment
  detail, active faults, monitoring status).
- Phase 6: hardening (retries already present in error classification;
  needs structured logging, RBAC, Docker, secrets manager, backups).
