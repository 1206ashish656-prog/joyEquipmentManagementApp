# Continuation prompt — Equipment Health Monitoring & Alerting

Paste this as your first message in a new session to resume this project
with full context, no re-discovery needed.

---

## What this is

A monitoring/alerting layer in front of a third-party equipment-management
web app (`jwintell.com` — a FastAdmin/ThinkPHP back office with no
documented API). It polls Equipment Management → Device Information,
converts raw fields into a canonical health state via configurable rules,
detects state changes exactly once per transition (never once per poll),
stores history, alerts subscribers by email, and serves a live web
dashboard. Repo: `c:\Users\hp\Desktop\self_projects\joyHealthMonitorinApp`
(git initialized, latest commit `4a586d0`).

**Read these two files first, in order — they carry all the detail this
prompt intentionally omits:**
1. [`README.md`](../README.md) — setup, run commands, architecture, current gaps.
2. [`docs/target_application_integration_spec.md`](target_application_integration_spec.md) — confirmed real target-app endpoints, login form fields, session-validity behavior, the CAPTCHA bug evidence.

A full narrative report (workflow diagrams, framework justification,
screenshots, tool appendix) also exists:
[`docs/report/Equipment_Monitoring_System_Report.pdf`](report/Equipment_Monitoring_System_Report.pdf) (v2, 25 pages) — source at `docs/report/report.html`.

## Build status

| Phase | Status |
|---|---|
| 1 — Target discovery | Done, confirmed live |
| 2 — Monitoring POC | Done, confirmed live |
| 3 — DB + state engine | Built, live-verified — **SQLite only, never run on real Postgres** |
| 4 — Alerting | Built, live-verified firing on real incidents — **no real email ever sent** (no SMTP creds) |
| 5 — Web dashboard | Built, live-verified with real HTTP traffic |
| 6 — Production hardening | **Not started** |

90 automated tests pass (`pytest tests/ --ignore=tests/tc_001_target_connection`).

**New, independent feature (2026-08-24): daily order summary.** Separate
`orders/` package (doesn't touch `monitoring/`/`services/`/`db/`) that
fetches Order Management → Order Information (a section the *original*
spec listed as a non-goal — now in scope by explicit request), filters to
`order_status=Completed AND delivery_status=Success`, and computes
aggregate + machine-wise daily summaries (orders, avg price, total
oranges, avg juice weight) into a CSV cache
(`data/order_summaries/daily_summary.csv`, git-ignored — real business
data) that skips already-computed dates. **Verified for exactly one day
so far (2026-08-23: 310 raw → 249 qualifying orders) — not yet
independently cross-checked against the target app's own UI, and not yet
backfilled across history (32,049 total orders exist ≈ 100+ days).** A
real, previously-unknown detail surfaced building this: the target's
`createtime` date-range filter uses **UTC+8 day boundaries**, confirmed
live, not UTC/IST/local time — see
`docs/target_application_integration_spec.md`. Next steps for this
feature: get the user's sign-off on the one verified day, then run the
full historical backfill (`python -m orders.backfill --start <earliest>
--end <today>`) — sequential, will take a while at ~1 request per ~100
orders — then eventually the CSV→database migration the user already
flagged as a later step.

## Critical context — do not relearn these the hard way

- **`AUTO_SOLVE_CAPTCHA=true` is currently enabled in `.env`.** This is a
  deliberate, *explicitly account-owner-authorized* reversal of the
  original project spec's own "never bypass CAPTCHA" requirement —
  specific to a confirmed bug in this one target (the CAPTCHA field is
  never actually validated against the image; any 4 chars pass). Default
  in `.env.example` is `false` and must stay that way for any other
  target. Don't quietly change this flag's default or remove the
  human-in-the-loop fallback path (`_submit_login_manual` in
  `target_client.py`) — both are intentional. Full disclosure is in
  README's "⚠️ CAPTCHA automation" section and report §4.1/§5.1/§7.4.
- **No Docker/Postgres or SMTP credentials were available during this
  build.** Everything was verified via SQLite (`DATABASE_URL` override —
  see `monitoring/config.py`/`db/base.py`) and a console-fallback email
  channel. **First thing to check in a new session: has that changed?**
  If Docker/Postgres is now available, closing that verification gap is
  the highest-value next step (see Pending below).
- **The dev sandbox's Bash/PowerShell tool has an intermittent
  permission classifier** that blocks some commands (especially ones
  touching the live target site or killing processes) and then lets an
  identical retry through seconds later. Just retry once or twice; don't
  try to route around a block via a different tool — that's explicitly
  against the harness's own instructions.
- **`.gitignore` excludes all of `data/`** (session cookies, discovery
  dumps, demo DB) — verified safe, but double-check before any manual
  `git add -A` after creating new files there (a session backup file
  once nearly got committed before the pattern was broadened to
  `data/storage_state/*`).
- **Real credentials live only in `.env`** (git-ignored): `TARGET_USERNAME`
  is `Refresha`; password and `WEB_SECRET_KEY` are set but not repeated
  here. Never hardcode or log credentials — `NotificationService`
  deliberately never logs SMTP passwords even on failure.
- **A worker (`--loop`) and dashboard (`uvicorn`, port 8123) may still be
  running in the background** from live demos, pointed at
  `data/demo.db` via `DATABASE_URL=sqlite:///data/demo.db` (not the
  `.env` `DB_*`/production path). Check with:
  ```powershell
  Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -match 'monitoring.worker|uvicorn' } | Select-Object ProcessId, CommandLine
  ```
  Demo DB contains 6 real equipment rows plus 2 clearly `[DEMO]`-labeled
  synthetic rows (added for screenshot/badge coverage) — don't mistake
  those for real data if inspecting it.
- **The whole app is currently running as a plain local process and
  fully stops if this machine sleeps** (confirmed: only traditional S3
  standby is available here, no Modern Standby — the CPU suspends
  entirely, so there's a real monitoring gap for the sleep's duration,
  not just a slowdown). Self-heals on wake (session re-auth is
  automatic), but this is a genuine limitation for anything beyond
  local/demo use. See README's "⚠️ Operational constraint" section — the
  🔖 bookmarked fix is always-on cloud hosting (below).

## Architecture (one paragraph)

`monitoring/lightweight_client.py` (plain httpx) handles ALL steady-state
polling — no browser. `monitoring/target_client.py` (Playwright) is used
only transiently, inside `worker.py`'s `_reauthenticate()`, when the
session is actually invalid. `services/health_engine.py` converts raw
fields to canonical health via `config/health_rules.yaml`.
`services/state_manager.py` is the alerting-correctness core (opens
exactly one incident per fault, escalates in place, resolves only on
explicit recovery). `services/alert_engine.py` +
`services/notification_service.py` handle who-gets-notified and delivery.
`db/models.py` uses only portable SQLAlchemy types so the schema runs
identically on SQLite (tests/demo) and PostgreSQL (production, via
`docker-compose.yml`). `backend/` is a server-rendered FastAPI + Jinja2
dashboard (chosen over Next.js — confirmed with the user; see README).

## Pending elements — suggested plan for next session

**Do first — check environment, don't assume last session's constraints still hold:**
1. Is Docker/Postgres available now? If yes: `docker compose up -d && python -m db.init_db`, run the worker + dashboard against it for real, and specifically re-verify the FK-enforcement/session-validity findings documented in the report hold on real Postgres too (SQLite quirks shouldn't apply, but confirm).
2. Are real SMTP credentials available? If yes: set `SMTP_*` in `.env`, trigger a real incident (or use the `[DEMO]` synthetic-row pattern from `docs/report/capture_screenshots.py`'s sibling scripts), confirm actual email delivery end to end.
3. 🔖 **Bookmarked: cloud hosting.** The app currently fully stops if its
   host machine sleeps (see "Critical context" above) — a real
   limitation, not yet acted on. If the user wants to move toward
   continuous/production operation, this is the natural next move:
   an always-on VM or managed container platform, using the existing
   `docker-compose.yml` as the starting point and closing the Postgres
   gap in the same move. Don't build this unprompted — raise it and let
   the user decide when they want to tackle it.

**Phase 6 — production hardening (pick based on what the user asks for; don't build all of this unprompted):**
- Alembic migrations (replacing `Base.metadata.create_all()`)
- Retry-with-backoff *within* a poll cycle (3 attempts, 5s/10s — currently a failure just waits for the next scheduled poll)
- Browser crash/restart recovery for the re-authentication path
- Structured logging, a health-check endpoint, Prometheus/Grafana
- Docker images for the worker + backend, Nginx reverse proxy
- Secrets manager in place of plain `.env`
- DB backups + documented restore procedure
- Rate limiting on login/API routes
- CSRF token on mutating forms (currently relies only on `SameSite=Lax`)
- In-app first-admin setup flow (currently CLI-only via `db/seed_admin.py`)

**Feature gaps (lower priority, need user direction):**
- Multiple monitoring profiles (more than one target account)
- Wire the target API's rich `this_fault` per-component detail into incidents (natural fit for a Postgres JSONB column, currently unused beyond the flat `fault_type` string)
- Confirm real-world fault/shortage vocabulary once (if) this account produces an actual non-"Normal" reading — current rule table is only validated against the spec's illustrative examples
- Configurable dashboard session lifetime (fixed 7 days currently)
- Optional Next.js frontend swap, if still wanted after using the server-rendered dashboard

## How to just run it (see README/report §9 for full detail)

```powershell
.venv\Scripts\activate
$env:DATABASE_URL = "sqlite:///data/demo.db"   # or omit for real Postgres via .env's DB_*
python -m monitoring.worker --loop              # terminal 1
uvicorn backend.main:app --host 127.0.0.1 --port 8123   # terminal 2
```
