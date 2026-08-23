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
| 1 — Target application discovery | Tooling ready (`discovery/inspect.py`); **you need to run it once** |
| 2 — Monitoring proof of concept | Implemented (`monitoring/poc_runner.py`) — selectors need confirming against your discovery output |
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

`.env` is git-ignored. Never commit real credentials.

## Step 1 — Discovery (run this first)

The target site's real DOM/network structure isn't known yet, so
`monitoring/selectors.py` is currently a best-effort placeholder. Run:

```bash
python -m discovery.inspect
```

This opens a **visible** browser at the login page. You log in yourself
(the CAPTCHA is never automated), navigate to Equipment Management →
Device Information yourself, then press Enter in the terminal. It saves,
under `data/discovery_output/` (git-ignored):

- `device_information.html` — full page HTML
- `device_information.png` — screenshot
- `network_log.jsonl` — captured XHR/fetch responses (check whether
  equipment data comes from a structured JSON endpoint rather than raw
  HTML — see requirement #15 in the spec)
- `summary.md` — auto-generated notes

It also saves the authenticated session to `data/storage_state/session.json`
(git-ignored) so the next step can reuse it without logging in again.

**After running this**, open the output and update the `TODO_DISCOVERY`
items in [`monitoring/selectors.py`](monitoring/selectors.py) to match
what you actually found (real input names, table structure, pagination
control, login-vs-authenticated markers).

## Step 2 — Monitoring POC

```bash
python -m monitoring.poc_runner
```

This launches **one** persistent browser session and demonstrates the
required behavior end to end:

1. Checks whether the session saved by discovery (or a prior POC run) is
   still valid — if so, **no login happens**.
2. Only if invalid/expired does it open the login page and hand off to you
   for the CAPTCHA (`HEADLESS=false` required for this step).
3. Opens Equipment Management → Device Information and extracts equipment
   records.
4. Normalizes and validates them (`EquipmentExtractor`), then prints them.
5. Waits `POLL_INTERVAL_SECONDS`, then repeats for `POC_ITERATIONS` cycles
   **reusing the same session** — this is the part worth watching: cycle 2+
   should log "Existing session is valid — reusing it (no login performed)."

Any technical failure (target unreachable, extraction error, session
expiring mid-run) is classified into a `MonitoringState`
(`AUTHENTICATION_REQUIRED` / `TARGET_UNAVAILABLE` / `EXTRACTION_ERROR` /
`MONITORING_ERROR`) and printed — it is never turned into a fake equipment
health value. See [`monitoring/models.py`](monitoring/models.py).

## Architecture (current pieces)

```
discovery/inspect.py        Phase 1: one-off manual DOM/network capture

monitoring/
  config.py                 env-driven settings (no hardcoded secrets)
  models.py                 MonitoringState, EquipmentRecord, exceptions
  selectors.py               ← the file to edit as the real site is confirmed
  browser_manager.py        owns the ONE persistent browser/context/page
  session_manager.py        session-aware auth: reuse valid session, only
                             authenticate when invalid/expired (core rule)
  target_client.py          all target-site interaction (nav, scrape)
  equipment_extractor.py    raw rows -> validated, normalized EquipmentRecord
  validator.py               "zero records" is a validation failure, not a
                             fleet-wide OFFLINE state (requirement #28)
  poc_runner.py              Phase 2 entry point
```

Deliberately **not yet built**: health engine (HEALTHY/WARNING/MALFUNCTION/
OFFLINE/UNKNOWN), Postgres persistence, incident/state-transition tracking,
alerting/notifications, FastAPI, and the Next.js dashboard. These are
Phase 3–5 and depend on confirmed field values from real discovery output
(e.g. what fault_type values actually appear) rather than the examples in
the spec.

## Next steps (not yet implemented)

- Phase 3: Postgres schema (`equipment`, `equipment_current_state`,
  `equipment_snapshot`, `fault_incident`, ...), `HealthEngine`, state
  transition detection.
- Phase 4: `AlertEngine` + `NotificationService` (email first), incident
  dedup/cooldown.
- Phase 5: FastAPI backend + Next.js dashboard (fleet overview, equipment
  detail, active faults, monitoring status).
- Phase 6: hardening (retries already present in error classification;
  needs structured logging, RBAC, Docker, secrets manager, backups).
