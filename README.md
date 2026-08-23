# Equipment Health Monitoring & Alerting

Session-aware monitoring layer for a third-party equipment management
application (Equipment Management → Device Information), built around two
principles: **authentication must be reused, not repeated** — see
[`monitoring/session_manager.py`](monitoring/session_manager.py) — and
**browser automation is only for the parts that actually need a browser.**
Steady-state polling uses a plain async HTTP client with the session's
cookies (see "No persistent browser" below); a real Chromium instance is
launched only for the rare CAPTCHA login itself, then closed immediately.

## Current scope

| Phase | Status |
|---|---|
| 1 — Target application discovery | **Done** — confirmed live against the real site on 2026-08-23. See [`docs/target_application_integration_spec.md`](docs/target_application_integration_spec.md). |
| 2 — Monitoring proof of concept | **Done** — [TC-001](tests/tc_001_target_connection/description.md) passes end to end: session reuse (no re-login on repeat runs), real equipment extraction via the target's own JSON API, 6/6 records normalized. |
| 3 — Database + state engine | **Implemented and live-verified** against the real target site (continuous `--loop` polling, real incidents opened/resolved) — but only against **SQLite**, not Postgres (see "Known gap" below). |
| 4 — Alerting | **Implemented, partially verified.** `NotificationService` (pluggable channel; console fallback when SMTP isn't configured) + `AlertEngine` (recipient resolution, message composition), wired into `monitoring/worker.py` and confirmed firing on real incidents during live runs. SMTP send path is unit-tested with `smtplib` mocked — **no real email has actually been sent** (no SMTP credentials available here). |
| 5 — Web dashboard | **Implemented and live-verified** — a real `uvicorn` process, hit with real HTTP requests, serving live poll data with 5s auto-refresh (see "No persistent browser" for the DB caveat). |
| 6 — Production hardening | Not started |

### No persistent browser: lightweight HTTP polling + browser-only-for-CAPTCHA

Earlier versions of `monitoring/worker.py` kept ONE Playwright browser
open for the worker's entire lifetime, navigating it every poll cycle —
functionally correct (no re-login), but a visible/running Chromium
process the whole time, and slower than necessary (~4-5s of page
navigation overhead per cycle for calls that are just XHR under the
hood).

`monitoring/lightweight_client.py` replaces that for steady-state
polling: it reads the cookies Playwright saved to
`data/storage_state/session.json` and makes plain `httpx` requests with
them directly — no browser process at all. `monitoring/worker.py`'s
`_reauthenticate()` is now the **only** place a Playwright browser gets
launched, and only when `LightweightTargetClient.is_session_valid()`
comes back `False` (session actually expired) — it opens (visibly, for
the human CAPTCHA step), logs in, saves the session, and closes
immediately. Verified live: a full `--loop` run against the real target,
watched with `Get-CimInstance Win32_Process`, produced **zero**
Playwright-owned `chrome.exe` processes across multiple poll cycles, all
served via plain `httpx` GETs, ~1-1.5s per cycle instead of ~4-5s.

`target_client.py` (the original Playwright-based client) still exists
and is still what `discovery/inspect.py`, `poc_runner.py`, and
`tests/tc_001_target_connection/` use — those are one-off/diagnostic
tools where a real browser is the point. The row-mapping logic both
clients share now lives in `monitoring/mapping.py`.

### Known gap: real Postgres and real SMTP still unverified

This dev environment has neither Docker/Postgres nor SMTP credentials
available. Everything DB- and email-touching is written against the real
`postgresql+psycopg2` driver / real `smtplib`, and verified via **70
passing unit/integration tests** (SQLite in place of Postgres, mocked
`smtplib`) — including one **live** run: a real `uvicorn` server + a real
`monitoring.worker --loop` process, both pointed at a local SQLite file
via the `DATABASE_URL` override (see below), continuously polling the
actual target site and correctly reflecting real incident open/resolve
events in the dashboard within seconds. What's *not* yet been exercised
is the Postgres driver itself and actual email delivery. Once available:

```bash
docker compose up -d
python -m db.init_db
python -m db.seed_admin --name "Your Name" --email you@example.com
python -m monitoring.worker --loop     # continuous polling against the real target
uvicorn backend.main:app --reload      # then open http://localhost:8000
```

### Frontend choice: FastAPI + Jinja2, not Next.js

The original spec named React/Next.js. Node/npm are available on this
machine, but given the scope already built, a server-rendered dashboard
(one deployable, no separate build/dev-server process, directly testable
with `TestClient`) was chosen instead — confirmed with the user before
building. The API layer (`backend/api/*.py`) is structured so a Next.js
frontend could be added later calling the same routes as JSON (see
`/api/monitoring/status` for the pattern) without touching `services/` or
`db/`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt   # or requirements.txt for runtime-only
python -m playwright install chromium
copy .env.example .env          # then fill in TARGET_USERNAME / TARGET_PASSWORD
```

`.env` is git-ignored. Never commit real credentials — see the warning in
`.env.example` itself. `WEB_SECRET_KEY` (signs dashboard login sessions)
needs a real value even for local dev — generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Database

```bash
docker compose up -d      # starts Postgres using .env's DB_* values
python -m db.init_db      # creates tables (idempotent)
python -m db.seed_admin --name "Your Name" --email you@example.com
                           # creates/resets the first dashboard admin login
                           # (separate from the target site's own credentials)
```

### Email alerts

Leave `SMTP_HOST` empty in `.env` to use the console fallback channel
(alerts are logged, not sent) — the default, since no SMTP is configured
here. Set `SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/
`SMTP_FROM_EMAIL` to send real email.

## Running it

```bash
python -m monitoring.worker
```

One full poll cycle against the live target — check session validity
(plain HTTP, no browser), extract equipment via the target's JSON API
(plain HTTP), evaluate health, persist snapshot/current-state,
open/escalate/resolve fault incidents, and notify subscribers on any of
those events. Add `--loop` to repeat forever on `POLL_INTERVAL_SECONDS`.
If there's no saved session yet (first run) or it's expired, a
**visible** Playwright browser opens just long enough for a human to
complete the CAPTCHA (`HEADLESS=false`), then closes — every cycle after
that reuses `data/storage_state/session.json` (git-ignored) via plain
HTTP requests, no browser at all. See "No persistent browser" above.

Any technical failure (target unreachable, extraction error, session
expiring mid-run) is classified into a `MonitoringState`
(`AUTHENTICATION_REQUIRED` / `TARGET_UNAVAILABLE` / `EXTRACTION_ERROR` /
`MONITORING_ERROR`), recorded on that cycle's `MonitoringRun` row, and
never turned into a fake equipment health value or a fake alert. See
[`monitoring/models.py`](monitoring/models.py).

```bash
uvicorn backend.main:app --reload
```

The dashboard: login, fleet overview with health counts, per-equipment
detail + history timeline, active faults list + detail, per-user alert
subscriptions, admin-only user management, monitoring status panel. Log
in with the account created by `db.seed_admin`.

`python -m tests.tc_001_target_connection.run` and
`python -m monitoring.poc_runner` still work as lighter-weight
connectivity-only checks (no DB) — see their own docs.

### Local demo without Docker/Postgres

`DATABASE_URL` overrides the `DB_*` settings entirely — point it at
SQLite for a quick local run:

```bash
DATABASE_URL="sqlite:///data/demo.db" python -m db.seed_admin --name "You" --email you@example.com --password "..."
DATABASE_URL="sqlite:///data/demo.db" python -m monitoring.worker --loop   # separate terminal
DATABASE_URL="sqlite:///data/demo.db" uvicorn backend.main:app --host 127.0.0.1 --port 8123
```

### Tests

```bash
pytest tests/ --ignore=tests/tc_001_target_connection -v   # 70 unit/integration tests, no live site/DB/SMTP needed
python -m tests.tc_001_target_connection.run                # live smoke test against the real target
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
  config.py                 env-driven settings, incl. DB/SMTP/web (no hardcoded secrets)
  models.py                 MonitoringState, EquipmentRecord, exceptions
  selectors.py               target URLs/API paths/field maps — the file to
                             edit first if the target site changes
  mapping.py                  raw-row -> canonical-field mapping shared by
                             BOTH clients below (target-specific, not
                             transport-specific)
  lightweight_client.py       plain-httpx client used for ALL steady-state
                             polling — no browser (see "No persistent
                             browser" above)
  browser_manager.py        owns a Playwright browser/context/page —
                             used only transiently now, for re-auth
  session_manager.py        session-aware auth: reuse valid session, only
                             authenticate when invalid/expired (core rule)
  target_client.py          Playwright-based client: login/CAPTCHA flow,
                             plus DOM-scrape fallback and the
                             discovery/POC/TC-001 tools' data path
  equipment_extractor.py    canonical rows -> validated EquipmentRecord
  validator.py               "zero records" is a validation failure, not a
                             fleet-wide OFFLINE state (requirement #28)
  worker.py                  real poll-cycle entry point: lightweight
                             fetch + DB + alerting; browser only via
                             _reauthenticate()
  poc_runner.py              Phase 2 connectivity-only demo (no DB, full
                             Playwright client throughout)

db/
  models.py                  equipment / equipment_current_state /
                             equipment_snapshot / fault_incident / users /
                             alert_subscription / monitoring_run (spec §16)
  base.py                    engine/session (sync SQLAlchemy — see its
                             docstring for why, given the async worker)
  init_db.py                 `python -m db.init_db` — creates tables
  seed_admin.py               `python -m db.seed_admin` — bootstraps/resets
                             the first dashboard login (chicken-and-egg fix)

config/
  health_rules.yaml           configurable health-evaluation rules (§7) —
                             edit this, not health_engine.py, to add values

services/
  health_engine.py            raw target fields -> canonical HealthState
                             (HEALTHY/WARNING/MALFUNCTION/OFFLINE/UNKNOWN)
  state_manager.py            the alerting-correctness core: detects a
                             malfunction exactly once, tracks it as one
                             incident until recovery (§8/§9)
  notification_service.py     pluggable delivery channel (email now;
                             console fallback when SMTP isn't configured)
  alert_engine.py             who gets notified (admins + subscriptions,
                             §19) and what the message says (§18) for the
                             incident events StateManager already found

backend/                      Phase 5 dashboard (FastAPI + Jinja2)
  main.py                     app wiring, startup DB init, auth redirect handler
  deps.py                     DB session / current-user / RBAC dependencies
  security.py                  password hashing + signed session cookies
                             (stdlib-only — no passlib/itsdangerous)
  templating.py                shared Jinja2Templates + health badge filters
  api/
    auth.py                    login/logout
    equipment.py                fleet overview ("/") + equipment detail
    alerts.py                   active faults + per-user subscriptions
    users.py                    admin-only user management
    monitoring.py                JSON /api/monitoring/status
  templates/, static/          Jinja2 HTML + one stylesheet, no build step

tests/
  conftest.py                  shared fixtures (in-memory SQLite + record factory)
  test_health_engine.py        unit tests for the rule table
  test_state_manager.py        incident dedup/escalation/resolution — reproduces
                             the spec's own "one alert, not four" worked example
  test_notification_service.py SMTP interaction mocked; credentials-never-logged check
  test_alert_engine.py         recipient-resolution matrix (admin/subscription/
                             severity/equipment-scoping/dedup)
  test_lightweight_client.py   cookie loading, redirect-to-login detection,
                             pagination, error classification — httpx.MockTransport,
                             no real network
  test_backend.py              FastAPI TestClient: auth, RBAC, all pages, subscription CRUD
  tc_001_target_connection/    numbered live test case: connect, authenticate
                             (reuse-first), extract, report malfunctions
```

Deliberately **not yet built**: Alembic migrations (using
`Base.metadata.create_all()` — fine for one evolving dev schema, revisit
before a real production cutover), a Next.js frontend (see "Frontend
choice" above) — Phase 6, and optionally Phase 5's original stack choice.

Two things worth knowing about the current health rules: their real-world
fault vocabulary is still unconfirmed (no device in this account has
reported a non-"Normal" `fault_type`/`material_shortage_status` yet — see
"still unconfirmed" in the integration spec), and the API's `this_fault`
field (rich per-component fault detail) isn't wired into `fault_type` yet,
only preserved in `EquipmentRecord.raw`/`FaultIncident`'s audit trail.

## Next steps (not yet implemented)

- Close the live-Postgres/SMTP verification gap once infra is available
  (see "Known gap" above).
- Phase 6: Alembic migrations, structured logging, retry-with-backoff
  within a poll cycle (spec §26), browser crash recovery, Docker images
  for the worker + backend, secrets manager, DB backups, rate limiting.
- Optional: swap the dashboard for a Next.js frontend against the
  existing `backend/api/` routes, if still wanted after seeing the
  server-rendered version.
- Wire the API's rich `this_fault` per-component detail into incident
  records once the health-rule vocabulary is confirmed against a real
  fault.
