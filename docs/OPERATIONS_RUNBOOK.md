# Operations Runbook

Quick, copy-pasteable commands for the day-to-day tasks the application
management team needs — not a technical deep-dive. For architecture,
setup from scratch, or how any of this works internally, see
[`README.md`](../README.md); for cloud hosting, see
[`DEPLOYMENT.md`](DEPLOYMENT.md).

**Before you start:** open a terminal in the project folder
(`c:\Users\hp\Desktop\self_projects\joyHealthMonitorinApp`), using
**Git Bash** (not PowerShell/cmd) — every command below relies on the
`VAR=value command` syntax, which only works in a bash-style shell.

All commands below assume the local database configured in `.env`
(Postgres, as of 2026-08-27 — see `DB_HOST`/`DB_NAME`/`DB_USER`/
`DB_PASSWORD`; no `DATABASE_URL` override needed, that's the default
already). If this has been deployed to the cloud (Railway), see
`DEPLOYMENT.md` instead — commands there run via `railway run` against
that separate, cloud-hosted Postgres database, not this local one.

---

## 1. Restarting the server

The app is two long-running processes: the **web dashboard** (what
people log into) and the **background worker** (polls the target
machines + refreshes order data every cycle). Restarting means stopping
both and starting both again — e.g. after a `.env` change, or if a
process seems stuck.

**Check what's currently running:**
```bash
tasklist | grep python
```
(or, in PowerShell: `Get-Process | Where-Object { $_.ProcessName -eq "python" }`)

**Stop everything:**
```bash
taskkill //IM python.exe //F
```
(This stops *every* running Python process on the machine — fine on a
dedicated app server, but check `tasklist` first if other Python
programs might also be running here.)

**Start the background worker** (equipment polling + order sync, one
process, run this first — leave this terminal open, or see the
"running in the background" note below):
```bash
python -m monitoring.combined_worker --loop
```

**Start the web dashboard** (in a second terminal):
```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8123
```

**Confirm it's back up:**
```bash
curl http://127.0.0.1:8123/healthz          # should print {"status":"ok"}
```
Then log in at `http://127.0.0.1:8123/` and check the dashboard shows
live equipment data.

**Running both in the background** (so they survive closing the
terminal), append `&` and redirect output to a log file you can check
later:
```bash
nohup python -m monitoring.combined_worker --loop > worker.log 2>&1 &
nohup uvicorn backend.main:app --host 127.0.0.1 --port 8123 > web.log 2>&1 &
```

---

## 2. Backfilling order data

Fills in historical order summaries for a date range (each date is
fetched once and cached — safe to re-run without duplicating data).

**A single missing date:**
```bash
python -m orders.backfill --date 2026-08-20
```

**A date range:**
```bash
python -m orders.backfill --start 2026-08-01 --end 2026-08-20
```

**Force re-fetch a date that's already been processed** (e.g. you
suspect the target's data changed after the fact):
```bash
python -m orders.backfill --start 2026-08-20 --end 2026-08-20 --force
```

Note: **today's date is deliberately never touched by backfill** — it's
kept continuously up to date instead by the background worker
(`monitoring.combined_worker`, section 1), since a day still in progress
would otherwise get permanently cached on a partial count.

**Manual backfill is now rarely needed after a restart.** The worker
automatically closes any gap left by downtime the moment it starts back
up (as of 2026-08-27) — if it was down when the day rolled over, the
last day it was tracking gets force-refreshed, and any fully-elapsed
days in between get backfilled, with no command needed. Use the manual
commands above for a genuinely large historical gap, or to force a
re-fetch of a specific date you suspect is wrong for another reason.

---

## 3. Adding test data in the equipment dashboard

Use `services.simulate_equipment_event` to add a machine that behaves
exactly like a real one (shows on the dashboard, opens/tracks
incidents, can trigger real alert emails) — without touching or
confusing real equipment data. **Every test equipment ID must start
with `TEST-`** — the tool refuses anything else, so it's always obvious
what's safe to delete afterward and it can never be mistaken for a real
machine.

**Add a healthy test machine:**
```bash
python -m services.simulate_equipment_event add --id TEST-001 --state healthy
```

**Transition it to a fault state** (also works to add one directly in a
fault state):
```bash
python -m services.simulate_equipment_event add --id TEST-001 --state malfunction
```
`--state` accepts `healthy`, `warning`, `malfunction`, or `offline`.
Add `--fault-type "Custom Fault Text"` to control what shows up in the
alert/dashboard for a malfunction (default: "Simulated Fault").

**Remove it when done** (deletes the equipment and all its history/
incidents — always clean up test data promptly):
```bash
python -m services.simulate_equipment_event remove --id TEST-001
```

---

## 4. Triggering the email alert process for testing

**Option A — trigger a single incident alert** (the "a machine just
went into malfunction" email), using the same tool as section 3 with
`--notify` added:
```bash
python -m services.simulate_equipment_event add --id TEST-001 --state malfunction --notify
```
This sends a **real email** (via whatever SMTP settings are in `.env`)
to every current alert recipient — same audience a real malfunction
would reach. It only sends on a genuinely *new* fault, matching
production behavior: running the same command twice in a row won't
send a second email for the same fault. Remember to `remove` the test
machine afterward (section 3).

**Option B — trigger the Critical Faults Digest** (the rollup table
email listing every machine currently in a Critical state):
```bash
python -m services.send_fault_digest
```
Sends nothing (and prints a message) if no machine is currently
Critical — that's a normal outcome, not an error. To force there to be
something to send, add a `TEST-` malfunction machine first (section 3),
then run this, then clean it up.

**Checking where an alert will go**, without sending anything: see
section 5's "Managing alert recipients" for the admin page that lists
current recipients, or check the log line after either command above —
it always prints/logs the exact recipient list it sent to.

---

## 5. Checking or modifying user database and access rights

Most user management is done through the dashboard itself — no command
line needed:

- **`/users`** (admin login required) — add a new user (name, email,
  password, role, and venue if the role is `venue_partner`), and
  activate/deactivate existing users. Three roles: `admin` (full
  access), `operations` (equipment monitoring only), `venue_partner`
  (their own venue's order data only).
- **`/alert-recipients`** (admin login required) — manage the flat list
  of extra email addresses that receive Critical alerts, independent of
  dashboard logins (e.g. an external ops contact who doesn't need a
  full account).

**Known gap:** there's currently no in-app way to change an existing
user's **role** or **venue assignment** after creation — only email/
password/active status. Two options if that's needed:

- **Recommended**: deactivate the old account on `/users` and create a
  new one with the correct role, or
- Edit the database directly (only if you're comfortable with this —
  ask a developer if unsure):
  ```bash
  python -c "
  from db import base as db_base
  from db.models import User
  db_base.init_engine()
  with db_base.get_session() as s:
      u = s.query(User).filter(User.email == 'someone@example.com').one()
      u.role = 'admin'  # or 'operations' / 'venue_partner'
      # u.venue_provider = 'Forum Kormangala'  # only meaningful for venue_partner
      print('Updated:', u.email, u.role)
  "
  ```

**Resetting someone's password** (no self-service flow exists yet — an
admin sets it directly):
```bash
python -c "
from db import base as db_base
from db.models import User
from backend.security import hash_password
db_base.init_engine()
with db_base.get_session() as s:
    u = s.query(User).filter(User.email == 'someone@example.com').one()
    u.password_hash = hash_password('NewStrongPassword123!')
    print('Password reset for', u.email)
"
```
Never share the new password over an insecure channel — hand it to the
person directly and have them change it after logging in (there's
currently no in-app "change my own password" page either — same
snippet, run by the account owner's request).

---

## Troubleshooting

- **`/healthz` doesn't respond / connection refused** — the web
  dashboard isn't running; see section 1.
- **Dashboard loads but shows no live updates** — the background worker
  isn't running, or crashed; check `worker.log` (if started per section
  1's backgrounded form) for a Python traceback, then restart it.
- **`database is locked` in the worker log** — this was the recurring
  reason equipment monitoring silently went dark: an unrelated crash in
  the orders loop, sharing one process with equipment monitoring, took
  the whole process down with it (`monitoring/combined_worker.py`'s
  deliberate "either loop crashing is fatal for the whole process"
  design). A WAL-mode fix (2026-08-26) reduced how often it happened but
  didn't eliminate it — moving to Postgres (2026-08-27) removes the
  actual cause: SQLite's single-writer model, which Postgres's proper
  concurrent-write support doesn't have. Should not recur now that the
  local dev/demo database is Postgres. If it somehow does, flag it to a
  developer — the coupled-crash *design* itself is still in place on
  purpose (see `CHANGELOG.md`'s 2026-08-27 entry for why), so a genuine
  fix there is a separate, deliberate decision, not an accident.
- **A command fails with `ModuleNotFoundError`** — you're likely not in
  the project's Python virtual environment. Prefix the command with the
  project's own Python instead, e.g.
  `./.venv/Scripts/python.exe -m orders.backfill ...`.
