# TC-001 — Target connection & equipment malfunction extraction

## Objective

Verify that the monitoring engine can, end to end, against the real target
application:

1. Establish (or reuse) an authenticated session.
2. Reach Equipment Management → Device Information.
3. Extract and normalize equipment records.
4. Identify which equipment is currently reporting a malfunction (i.e.
   `fault_type` not in the "no fault" set, or `network_status` offline).

This is a connectivity/extraction smoke test — it does **not** exercise the
health engine, database, incidents, or alerting (not built yet; Phase 3+).

## Preconditions

- `.env` populated with real `TARGET_USERNAME` / `TARGET_PASSWORD`.
- `HEADLESS=false` (a human may need to complete the CAPTCHA once).
- Chromium installed (`python -m playwright install chromium`).

## Steps

```
python -m tests.tc_001_target_connection.run
```

1. Script checks for an existing valid session (`is_session_valid()`).
2. If invalid/expired, opens the login page, prefills credentials if
   selectors match, and waits (polling `page.url`, up to
   `AUTH_MANUAL_TIMEOUT_SECONDS`) for a human to complete the CAPTCHA and
   submit — never automated.
3. Persists the session to `data/storage_state/session.json` on success.
4. Opens Device Information and extracts all equipment rows.
5. Validates and normalizes them (`EquipmentExtractor`).
6. Prints a summary: total records, and which ones look like a malfunction
   (fault_type present / network offline), preserving the exact raw fault
   value per requirement #10.
7. Writes the full result (all records + summary) to
   `tests/tc_001_target_connection/results/<timestamp>.json`.

## Expected result

- Script completes without raising `MonitoringError`.
- At least 1 equipment record retrieved (zero records = FAIL, per the
  "never treat absence of data as zero machines" rule — see
  `monitoring/validator.py`).
- Each record has non-empty mandatory fields (id, code, name, status,
  network_status, fault_type, material_shortage_status).
- Malfunction summary correctly lists only records whose raw fault_type
  isn't a recognized "no fault" value.

## Result

_(filled in after each run — see the most recent file under `results/` for
full data; this section tracks pass/fail history only)_

| Run | Date (UTC) | Outcome | Notes |
|---|---|---|---|
| 1 | 2026-08-23 09:48:11Z | FAIL | Auth succeeded (session established + persisted, human completed CAPTCHA — see log). Extraction failed: sidebar "Device Information" click didn't expand (slide-toggle submenu, element present but not visible). 0 records. |
| 2 | 2026-08-23 09:55:57Z | **PASS** | After switching to the confirmed JSON list API + direct URL navigation (see `docs/target_application_integration_spec.md`). Session reused from run 1 (no re-login/no CAPTCHA — confirms core session-reuse requirement). 6/6 real equipment records retrieved and normalized; 3 correctly flagged offline by the test heuristic (`network_status == "Offline"`); 0 currently reporting a fault_type malfunction (this account has none active right now). |

