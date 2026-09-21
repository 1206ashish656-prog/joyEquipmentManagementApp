# Equipment Health Monitoring & Alerting

Session-aware monitoring layer for a third-party equipment management
application (Equipment Management → Device Information), built around two
principles: **authentication must be reused, not repeated** — see
[`monitoring/session_manager.py`](monitoring/session_manager.py) — and
**browser automation is only for the parts that actually need a browser.**
Steady-state polling uses a plain async HTTP client with the session's
cookies (see "No persistent browser" below); a real Chromium instance is
launched only for the rare CAPTCHA login itself, then closed immediately.

Every user prompt that shaped this build is logged verbatim (where
preserved) in [`docs/prompt_logs.md`](docs/prompt_logs.md) — append new
ones there rather than starting a second log.

## Current scope

| Phase | Status |
|---|---|
| 1 — Target application discovery | **Done** — confirmed live against the real site on 2026-08-23. See [`docs/target_application_integration_spec.md`](docs/target_application_integration_spec.md). |
| 2 — Monitoring proof of concept | **Done** — [TC-001](tests/tc_001_target_connection/description.md) passes end to end: session reuse (no re-login on repeat runs), real equipment extraction via the target's own JSON API, 6/6 records normalized. |
| 3 — Database + state engine | **Implemented and live-verified** against the real target site (continuous `--loop` polling, real incidents opened/resolved) — but only against **SQLite**, not Postgres (see "Known gap" below). |
| 4 — Alerting | **Implemented, partially verified.** `NotificationService` (pluggable channel; console fallback when SMTP isn't configured) + `AlertEngine` (recipient resolution, message composition), wired into `monitoring/worker.py` and confirmed firing on real incidents during live runs (as recently as 2026-08-24: simulated a fresh MALFUNCTION end-to-end and watched the exact recipient list + message get composed). Recipients now include an admin-editable flat email list (`/alert-recipients`, no dashboard account needed) alongside admins-always and per-user subscriptions — see "Alert Recipients" below. SMTP send path is unit-tested with `smtplib` mocked — **no real email has actually been sent** (no SMTP credentials configured here yet). |
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

### ⚠️ CAPTCHA automation — an explicit, deliberate deviation from the original spec

**The original spec for this project explicitly prohibits this** (§3:
"must NOT be designed around bypassing or defeating CAPTCHA"; §33 lists
"Attempt to bypass CAPTCHA" as a non-goal), and that stance is the sane
default. `AUTO_SOLVE_CAPTCHA` in `.env` exists only because, on
2026-08-23, the account owner identified and explicitly authorized
exploiting a bug in this specific target: its CAPTCHA field is never
actually validated against the code shown in the image — any 4
alphanumeric characters are accepted. With the flag enabled,
`authenticate()` fills that field with a random 4-character string and
submits immediately; no human, no visible browser, no wait. Verified
live from a fully deleted session: `is_session_valid()` correctly
detected no session, `_reauthenticate()` completed with zero human input,
and the next poll cycle succeeded (6/6 records).

This is **default OFF** (`AUTO_SOLVE_CAPTCHA=false` in `.env.example`)
and should stay that way for any other target. The human-in-the-loop path
(`_submit_login_manual`) is fully intact and is what runs whenever this
flag is left at its default — see `target_client.py`'s `authenticate()`.

Fixing this also surfaced a real bug: `LightweightTargetClient
.is_session_valid()` previously probed the dashboard shell page, which
(confirmed live) returns HTTP 200 with **no redirect even with zero
cookies at all** — a false-positive trap for "no session yet" that let a
poll cycle proceed straight to a failed data fetch instead of
re-authenticating first. It now probes the actual list API
(`limit=1`) and checks the JSON shape directly — see
`docs/target_application_integration_spec.md`'s "Session validity"
section for the full story.

### Known gap: real Postgres and real SMTP still unverified

This dev environment has neither Docker/Postgres nor SMTP credentials
available. Everything DB- and email-touching is written against the real
`postgresql+psycopg2` driver / real `smtplib`, and verified via **112
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

### ⚠️ Operational constraint: the host machine must not sleep

Both the worker and the dashboard are plain OS processes with no
supervisor keeping them alive across power states. If the machine
they're running on goes into **sleep/standby (S3) or hibernate, the
processes are fully suspended — not slowed, paused** — no polling, no
alerting, no dashboard responses to anyone, for the entire duration.
Confirmed on the current dev machine (Windows, `powercfg /a`): only the
traditional S3 standby is available (no "Modern Standby"/S0 Low Power
Idle), which is the more severe case — the CPU stops entirely, so no
background activity survives sleep at all.

On wake, the worker resumes on its own (no crash, no corrupted state) and
self-heals: if the target session expired during the gap,
`is_session_valid()` detects it on the next poll and `_reauthenticate()`
runs automatically (with `AUTO_SOLVE_CAPTCHA=true`, no human needed). Any
request that fails because the network hasn't reconnected yet is
classified as `TARGET_UNAVAILABLE`, never a fake equipment fault — but
there is a genuine **monitoring gap for the sleep's entire duration**,
which defeats the point of continuous monitoring.

**This is only survivable for local/demo use.** For anything meant to run
continuously, this needs to run somewhere that doesn't sleep — see the
cloud-hosting bookmark in "Next steps" below.

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
If there's no saved session yet (first run) or it's expired, a Playwright
browser opens just long enough to log in, then closes — every cycle
after that reuses `data/storage_state/session.json` (git-ignored) via
plain HTTP requests, no browser at all. With `AUTO_SOLVE_CAPTCHA=false`
(the sane default for any target other than the one this was explicitly
authorized against — see "⚠️ CAPTCHA automation" above), that browser is
**visible** (`HEADLESS=false`) and waits for a human to complete the
CAPTCHA. With `AUTO_SOLVE_CAPTCHA=true`, it runs fully headless with zero
human input. See "No persistent browser" above.

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

**Note (2026-08-27):** this project's own local dev/demo environment now
runs on a real local Postgres install instead of SQLite (see
`CHANGELOG.md`'s 2026-08-27 entry) — SQLite's single-writer model was
causing intermittent `database is locked` crashes that took equipment
monitoring down along with the unrelated orders loop. The instructions
below are kept as a genuinely valid, still-supported *quicker* path for
anyone setting this project up fresh without wanting to install Postgres
first — just be aware it carries that same concurrency risk under real
sustained load (a single quick demo run is fine).

`DATABASE_URL` overrides the `DB_*` settings entirely — point it at
SQLite for a quick local run:

```bash
DATABASE_URL="sqlite:///data/demo.db" python -m db.seed_admin --name "You" --email you@example.com --password "..."
DATABASE_URL="sqlite:///data/demo.db" python -m monitoring.combined_worker --loop   # separate terminal — equipment polling + today's orders, one process
DATABASE_URL="sqlite:///data/demo.db" uvicorn backend.main:app --host 127.0.0.1 --port 8123
```

(`monitoring.worker` and `orders.realtime_worker` still work standalone
in their own separate terminals too, exactly as before — useful for
debugging one loop in isolation — but `monitoring.combined_worker` is
the normal way to run both now, matching the cloud deployment's own
`combined-worker` service.)

### Tests

```bash
pytest tests/ --ignore=tests/tc_001_target_connection -v   # 112 unit/integration tests, no live site/DB/SMTP needed
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

## Order summary feature

A separate, independent feature from equipment health monitoring: a
business summary (order count, price, oranges used, juice weight)
sourced from Order Management → Order Information — a section the
original spec explicitly listed as a non-goal, now in scope by explicit
request. Lives in its own `orders/` package plus two new tables in the
existing DB (`db/models.py`'s `OrderSummary`/`OrderSummaryRun`); nothing
in `monitoring/`/`services/` was touched.

```bash
python -m orders.backfill --date 2026-08-23              # single day
python -m orders.backfill --start 2026-08-01 --end 2026-08-23   # range
```

**Storage grain** (redefined from an earlier CSV-based v1 after user
feedback): one row per `(date, device_app, price, pay_type)` — the
*finest* grain the feature needs, not a pre-aggregated one. If price
changes mid-day on a machine, that's two separate rows for that machine,
one per price, by design ("if there are 3 machines active and price was
altered mid-day then I expect 2 separate entries per machine, one for
each price"). Every other view — full aggregate, machine-wise,
price-wise, pay-type-wise, or any combination — is a rollup computed
from these rows at query/render time (`orders/rollup.py`), not a
separately stored table, so no view is ever missing because it wasn't
pre-computed. Storage moved from the originally-planned CSV straight to
the database for this redesign — a CSV can't reasonably support the
flexible period/breakdown queries the UI needs.

- **Filter applied before any number is computed** (confirmed with the
  user): only orders where `order_status == "Completed"` AND
  `delivery_status == "Success"` count.
- **"Already processed" tracking**: `OrderSummaryRun` (one row per date,
  mirrors `MonitoringRun`'s role for the equipment poller) — a quiet day
  with zero qualifying orders still gets a `SUCCESS` row, so it's never
  endlessly retried; a fetch failure gets a `FAILED` row instead of a
  fabricated zero, and is retried on the next run. `backfill` only ever
  fetches sequentially, one date at a time, skipping dates already marked
  `SUCCESS` (confirmed live: re-running the same `--date` produces zero
  HTTP traffic).
- **UI**: a new "Order Summary" dashboard tab
  (`backend/api/orders.py` + `backend/templates/order_summary.html`) —
  period selector (Daily/Weekly/Monthly/YTD + an "as of" date), checkboxes
  to break down by machine/price/pay type independently, a Chart.js
  line chart (orders per day, aggregated or one line per machine — via
  CDN, no build step), and the breakdown table. All server-rendered and
  computed from the same `orders/rollup.py` at request time.
- **Verified live end to end**, including the actual rendered dashboard
  page (not just the CLI): 2026-08-23 (the last fully-complete UTC+8 day)
  → 310 raw orders, 249 passed the filter, 3 groups (Gravity/NEXUS/PNR,
  no price change that day so one group each), 47/120/82 orders — matches
  across the CLI output, the DB tables, and the live dashboard screenshot.
  **Not yet independently verified against the target app's own UI**, and
  **not yet backfilled beyond that one day** (32,049 total orders exist —
  see the integration spec) — both flagged as next steps.
- A real, previously-undocumented detail this surfaced: the target's own
  `createtime` date-range filter uses **UTC+8 (China Standard Time) day
  boundaries**, not UTC/IST/local time — confirmed by checking three
  consecutive days' earliest/latest order timestamps. Getting this wrong
  would have silently shifted every day's numbers. See
  `docs/target_application_integration_spec.md`.
- **Full historical backfill completed** 2026-05-02 through 2026-08-23
  (114 days, zero failures) — see `CHANGELOG.md`'s 2.0.0 entry.
- **This app's own reporting day boundary is now IST, not the target's
  UTC+8** — see the next section. The UTC+8 finding above is still true
  and still relied on internally, just no longer what gets reported.

### Vendor (venue_partner) view: glasses-sold only, plus a downloadable report

Two related, explicit-request changes narrowing what a venue partner
sees on `/orders/summary`, on top of the RBAC scoping above:

**Trimmed to glasses-sold only (2026-09-10)** — the KPI tile row and
Breakdown table show a venue partner exactly ONE figure, the
glasses-sold count (`number_of_orders` — this vending-machine business
dispenses one cup per qualifying order, there's no separate
multi-glass-order concept anywhere in the data). Avg Price/Total
Oranges/Avg Juice Weight are now admin-only alongside the
already-admin-only Revenue/Oranges-per-Glass. Admin's own view is
unchanged. Template-only change (`order_summary.html`) — no route/data
logic touched, since the underlying rows were already computed
identically for both roles; only which fields render was ever
role-gated.

**Downloadable sales report (2026-09-22)**, per explicit request:
"provide report downloading option to vendors in vendor view. If
datewise data is selected, then provide datewise data. If ytd is
selected then, return monthwise sales summary." Reuses the Senior
Management Report's exact `ReportJob`/background-worker infrastructure
(same PENDING → RUNNING → SUCCESS/FAILED lifecycle, same "generation
never blocks a web request" guarantee) rather than building a parallel
system, but renders a **deliberately separate, minimal** object —
`services/management_report.py`'s `VendorSalesReport` — that has no
revenue/cost/profit/venue-comparison/downtime field **at all**, not
just those fields hidden at the template layer like the KPI-tile fix
above. The reasoning: a future template bug can't leak what the object
never carries in the first place — the same "can't leak what isn't
there" logic already behind the PayU/SMTP credential-never-logged
rules elsewhere in this app.

- `ReportJob` gained a `venue_provider` column (`NULL` = the existing
  admin-initiated report; set = a vendor-initiated one, always with
  `equipment_id=NULL`) — `services/report_job_worker.py` branches on it
  to call `render_vendor_report_pdf()` instead of
  `render_management_report_pdf()`.
- The report always includes a **Monthly Breakdown** table (glasses
  sold per month) — this is what naturally satisfies "if YTD selected,
  return monthwise sales summary," since a YTD-shaped date range simply
  produces many months in that same, always-present table; no separate
  YTD-specific code path exists.
- The **same "Include date-wise sales breakdown" checkbox** as the
  admin report adds a **Date-wise Sales** table (glasses sold per day)
  — satisfies "if datewise data is selected, then provide datewise
  data" directly.
- `GET /orders/summary/report/{job_id}/download` enforces ownership
  (`job.venue_provider == user.venue_provider`) — a venue partner can
  never download another vendor's report by guessing a job ID; both a
  wrong owner and a nonexistent job collapse to the same redirect, not
  a distinguishable error.
- New venue-scoping resolution (`services/management_report.py`'s
  `_venue_machines` + `build_vendor_report`) is a deliberate duplicate
  of `backend/api/orders.py`'s own `_venue_machines`, not a shared
  import — same "small local helper per module" convention already
  used by `venues.py`/`users.py`'s own separate venue-name helpers.

Migration note for an **existing** deployment (same "no Alembic"
situation as elsewhere): `venue_provider` is a new column on the
existing live `report_job` table —
```sql
ALTER TABLE report_job ADD COLUMN venue_provider VARCHAR(255);
```
A fresh install gets this for free via `create_all()`.

### Reporting timezone: IST, not the target's UTC+8

Per explicit request (2026-08-24): every day boundary this app reports
— `order_date` on each order, every stored `OrderSummary`/
`OrderSummaryRun` date, every Order Summary period/breakdown, "today" for
the realtime worker — is computed in **IST (India Standard Time,
UTC+5:30)**, via `orders/mapping.py`'s `IST_TZ`. The target's own UTC+8
convention (`TARGET_TZ`, same file) is unchanged and still real, but is
now purely an implementation detail of how the target is queried, not
what gets shown.

Since IST is 2.5 hours behind UTC+8, **one IST calendar day always spans
parts of two of the target's own UTC+8 days** — there's no way to ask
the target's RANGE filter for "one IST day" directly. `orders/client.py`'s
`fetch_day(ist_date_str)` handles this: it queries the target for BOTH
of its UTC+8 days that could contain an IST-day order (`ist_date_str`
and the day after), merges the results, then keeps only the orders whose
own computed `order_date` (IST-based) actually equals `ist_date_str` —
the target's bucketing is a coarse over-fetch, not the final word on
which day an order belongs to.

**The full order history was re-backfilled under IST boundaries the same
day** (`python -m orders.backfill --start 2026-05-02 --end 2026-08-23
--force`) so there's no seam in the data between "old UTC+8-bucketed
days" and "new IST-bucketed days" — every stored day reflects the same
convention. This roughly doubles the target-side requests per historical
day (two UTC+8-day fetches instead of one), so a re-backfill like this
takes noticeably longer than the original one.

### Today's order data (`orders/realtime_worker.py`)

`orders/backfill.py` deliberately only processes a date once it's fully
elapsed (see `db/models.py`'s `OrderSummaryRun` docstring) — a day still
accumulating orders would otherwise get permanently cached on a partial
snapshot the first time anyone looked at it. The consequence: **today
never shows up in Order Summary until backfill runs for it tomorrow,**
which looks like a bug ("why is today's data missing?") but is the
correct behavior for that script's caching model.

`orders/realtime_worker.py` is the deliberate exception — it always
targets "today" (recomputed every cycle, in **IST**, not the target's
own UTC+8 or server-local time) and **unconditionally overwrites**,
whether or not that date already has a `SUCCESS` row from an earlier
cycle:

```bash
DATABASE_URL="sqlite:///data/demo.db" python -m orders.realtime_worker --loop              # every 5 min, forever
DATABASE_URL="sqlite:///data/demo.db" python -m orders.realtime_worker --loop --interval 120
DATABASE_URL="sqlite:///data/demo.db" python -m orders.realtime_worker --once               # refresh today once and exit
```

Run this as a fourth long-lived background process alongside the
equipment worker and the dashboard (see "How to just run it" below) —
it's intentionally a separate script from `monitoring/worker.py`, same
architectural split as the rest of `orders/` from `monitoring/`/
`services/`. When the IST date rolls over mid-loop, the day that just
ended gets **one final refresh** before the new day starts being
tracked, so orders placed between the last tick and midnight aren't
silently lost (`orders/realtime_worker.py`'s `run_cycle()`). Live-verified
2026-08-24: manually confirmed the Order Summary tab was missing today's
data, ran `--once`, watched it appear (215 raw → 172 qualifying orders,
3 groups) in both the DB and the dashboard within seconds.

## Access control (RBAC)

Three roles (`db/models.py`'s `UserRole`), enforced at the route level via
`backend/deps.py`'s `require_operations`/`require_venue_partner`/
`require_admin` FastAPI dependencies — not just hidden nav links:

| Role | Can see |
|---|---|
| `admin` | Everything, unrestricted. |
| `operations` | Equipment monitoring only: Dashboard, Active Faults, My Alerts, `/api/monitoring/status`. No order data, no user management. |
| `venue_partner` | Order Summary only, scoped to their own venue's machine(s) — see below. No equipment/dashboard access. |

A venue partner's scope comes from two things together: `User.venue_provider`
(which venue they represent) and the new **`venue_mapping`** table
(`machine_name` → `venue_provider`, matched case-insensitively), seeded via:

```bash
python -m db.seed_venue_mapping          # seeds the known 2026-08-24 mapping:
                                          #   PNR     -> PNR Felicity
                                          #   NEXUS   -> Forum Kormangala
                                          #   Gravity -> Prestige Tech Park
python -m db.seed_venue_mapping --set "Warehouse=Some Venue"   # add/override one
```

`backend/api/orders.py` filters `OrderSummary` rows to
`device_app IN (machines mapped to this user's venue_provider)` before any
rollup runs — a venue partner never receives another venue's rows over the
wire, not just a UI that hides them. A venue partner with no
`venue_provider` set, or one that maps to zero machines, gets an explicit
"no venue assigned" message rather than an empty table that looks like a
bug. Post-login redirect is role-aware too (`home_url_for` in
`backend/deps.py`): a venue partner lands on `/orders/summary`, not `/`
(which they can't see).

Admins assign role + venue when creating a user (`/users`, admin-only) —
the "Venue Partner" role reveals a venue dropdown that's required before
the form will submit.

**Venue dropdown fixed to include the `Venue` master list too
(2026-09-22)** — `backend/api/users.py`'s `_venues()` originally listed
only `VenueMapping.venue_provider` values, so a venue added at `/venues`
(the separate master-list page added later for Recurring Costs, see
"Senior Management Report"'s neighbor section below) never appeared
here at all until a machine was also separately mapped to it via
`db.seed_venue_mapping` — a real reported bug ("existing venues not
appearing in the dropdown despite the /venues endpoint showing them").
`_venues()` now returns the **union** of active `Venue.name` values and
`VenueMapping.venue_provider` values, so a freshly-added venue is
selectable immediately; a venue_partner assigned to one with no machine
mapped yet still gets the existing "No machine is currently mapped to
your venue" callout on Order Summary rather than a silent blank page.

**Auto-sync a Venue to a same-named machine (2026-09-22, per explicit
request: "sync the venues available on venues database automatically
maps to same machine")** — the fix above made a venue *selectable*
immediately, but the underlying `VenueMapping` row (what Order Summary
actually reads) still had to be created by hand via
`db.seed_venue_mapping`, which is exactly what a follow-up real report
hit: a venue partner assigned to "Navi" still saw "no machine mapped."
New `services/venue_machine_sync.py`: whenever there is **exactly one**
`Equipment` row whose name matches a venue's name case-insensitively,
and that machine isn't already mapped to some other venue, a
`VenueMapping` row is created automatically — no CLI step needed for
the common case of a venue named after its one machine (increasingly
true as machines get renamed to their location on the target app
itself, e.g. "Navi"). Deliberately conservative: zero or multiple
name matches are left alone (never guessed), and an already-mapped
machine is never silently reassigned — a venue like "PNR Felicity"
(machine "PNR", a different string) still needs the manual
`db.seed_venue_mapping --set "PNR=PNR Felicity"` path, same as before.
Wired in three places: `GET /venues` runs it for every venue on the
page (**self-healing** — an existing gap like "Navi" fixes itself the
next time an admin just opens the page, no backfill command needed),
and both create/edit-venue routes run it immediately for instant
feedback. `/venues`' table gained a **"Mapped Machine(s)"** column
showing the real `VenueMapping` state (or a "⚠ No machine mapped"
badge) directly, instead of that relationship being invisible outside
a database query.

Existing deployments upgrading from before this feature need a manual
schema patch (this project has no Alembic yet — see Phase 6 in
`docs/NEXT_SESSION_PROMPT.md`): `ALTER TABLE users ADD COLUMN
venue_provider VARCHAR(255)`, then `python -m db.seed_venue_mapping`
(which also creates the new `venue_mapping` table via `create_all()`).

**Super admin (2026-08-28)**: `User.is_super_admin` gates changing
ANY user's role to/from `admin` (`/users/{id}/edit`, `backend/api/users.py`)
— a regular admin can still edit everything else on any user, and can
freely switch a non-admin between `operations`/`venue_partner`; only a
super admin can touch the `admin` role itself. Same "no Alembic"
situation as `venue_provider` above — an **existing** deployment needs
the same kind of manual patch: `ALTER TABLE users ADD COLUMN
is_super_admin BOOLEAN NOT NULL DEFAULT FALSE`, then re-run
`python -m db.seed_admin --email <the designated super admin's email> --name "..." --password "..."`
to mark that account as the (one, explicitly chosen) super admin. A
fresh install gets the column for free via `create_all()`.

**Staff email (2026-08-29)**: `Staff.email` (optional) lets a staff
member be quick-added to the flat Alert Recipients list
(`/alert-recipients`) without retyping their email, and lets
offboarding (`/staff/{id}/offboard` or clearing/setting the end date via
`/staff/{id}/edit`) auto-deactivate that recipient row
(`backend/api/staff.py`'s `_deactivate_alert_recipient_for`) — a left
staff member stops getting equipment alerts through their staff email
without an admin having to remember to remove them separately. Same
"no Alembic" situation: an **existing** deployment needs `ALTER TABLE
staff ADD COLUMN email VARCHAR(255)`. A fresh install gets the column
for free via `create_all()`.

**Update 2026-08-29**: the designated super admin is now
`support@refresha.in` (`db/seed_admin.py`'s `SUPER_ADMIN_EMAIL`) —
reassigned from the original seed account, `1206ashish656@gmail.com`,
which was deactivated (`User.active = False`) in the same change, per
explicit request. A deactivated account is fully locked out (login and
every session-authenticated route check `User.active` —
`backend/api/auth.py`, `backend/deps.py`) regardless of its role or
`is_super_admin` flag; the flag itself was deliberately left set on the
deactivated account (harmless while inactive, and avoids rewriting
history for no functional reason) rather than cleared.

## Cost Management and Staff & Leave Management (admin-only)

Two more admin-only tabs, both gated by the existing `require_admin`
dependency (not a new role — `operations`/`venue_partner` get `403` on
every route below, same as `/users`).

**Cost Management** (`/costs`, `costs/` package) — logs raw-material/
operating costs against a standard category list (`db/models.py`'s
`STANDARD_COST_CATEGORIES`: Oranges, Glass, Straws, Sealing Films, Staff
Salaries, Rent, Cleaning Items) or a custom one — picking "Others" in the
form asks for the real category name, and *that* text is what's stored,
not the literal word "Others". Vendor name is skipped entirely for Staff
Salaries (not applicable); for every other category, a blank vendor
becomes `"UNSPECIFIED"` (`db/models.py`'s `UNSPECIFIED_VENDOR`) rather
than a bare `NULL` — a deliberate, filterable placeholder, not silently
losing the fact that no vendor was given. Summarized over
Daily/Weekly/Monthly/YTD **or a user-provided custom date range** (the
`period=custom&start=…&end=…` option — `backend/period_utils.py`, shared
with Order Summary, which now also supports it), with independent
breakdown by category/vendor/item name (`costs/rollup.py`, sums `amount`
and counts entries — no weighted averages needed here, unlike order
pricing).

The same page also lets the admin **filter to a single category, vendor,
or item** (narrows both the rollup and a "Raw Entries" table of the
underlying individual rows), and **edit or delete any logged entry**
(`/costs/{id}/edit`, `/costs/{id}/delete`) — editing reuses the exact
same category/vendor resolution rules as creating a new entry, so
switching an edited row to "Others" or to "Staff Salaries" behaves
identically to doing so on the add form. The Raw Entries table itself is
**hidden by default** — a "Raw data: Show / Hide" radio pair on the page
controls it explicitly; the rollup breakdown and filters work either way.

**Staff & Leave Management** (`/staff`, `staff/` package) — a staff
roster (name, **department/sub-department** — free text, e.g.
"Operations" / "Logistics", suggested via a browser `<datalist>` of
previously-used values rather than a hard-coded list — employment start
date, optional end date — offboarding sets the end date rather than
deleting the row, so past leave stays attributable) and leaves logged by
the admin on a staff member's behalf, as a `[start_date, end_date]`
period rather than one row per day. `staff/leave_summary.py` computes
days-on-leave per staff for a selected month, **clipping a leave that
spans a month boundary to the month being viewed** (so it's correctly
split between two months' totals, not double-counted or misattributed),
and highlights anyone with more than 2 days that month in the UI.

Every roster row has an **Edit** link (`/staff/{id}/edit`) that can
change any field, including clearing the employment end date to undo a
mistaken "Mark as left" — a cleared end date makes the staff member
active again, and the "Mark as left" action reappears for them on the
roster (same `not employment_end_date` check that already drove it).

**Editable/deletable leaves + half-day leave (2026-09-10)** — every row
in the Recent Leaves table now has its own **Edit** (`/staff/leaves/
{id}/edit`, can reassign staff/dates/reason and toggle half-day) and
**Delete** link, matching the roster's own edit/delete pattern, so a
mis-logged leave no longer has to stay wrong forever. `StaffLeave`
gained `is_half_day` — a half-day leave is always a single day
(`start_date == end_date`, enforced server-side regardless of what a
form submits) counted as **0.5 days** rather than 1 in
`staff/leave_summary.py`'s month totals, so e.g. two ordinary leave
days plus one half-day correctly reads 2.5 (and correctly still trips
the ">2 days" highlight). The "Log a Leave" and "Edit Leave" forms
hide the End date field via a small inline script when "Half day" is
checked (mirroring Inventory's cartons/pieces toggle), syncing it to
the start date automatically rather than leaving a stale value the
backend then has to override silently.

Migration note for an **existing** deployment (same "no Alembic"
situation as above): `is_half_day` is a new column on the existing
live `staff_leave` table —
```sql
ALTER TABLE staff_leave ADD COLUMN is_half_day BOOLEAN NOT NULL DEFAULT FALSE;
```
A fresh install gets this for free via `create_all()`.

**Recurring Costs (2026-08-30)** — Cost Management gained a "Recurring
Costs" section (`services/recurring_costs.py`) that derives what's due
for a chosen month directly from two live lists rather than an admin
retyping the same figures every month: one **Rent** entry per active
**Venue** (new admin page, `/venues`, `db/models.py`'s `Venue` — its own
master list, distinct from `VenueMapping`'s machine→venue-name scoping
mapping, since that has no "one row per venue," rent, or active
concept) with a monthly rent set, and one **Staff Salaries** entry per
active staff member with `Staff.monthly_salary` set. The candidate list
always reflects the current Venue/Staff tables — onboard a venue or
staff member, or edit their rent/salary, and the next "Generate" run
picks it up automatically; offboarding a staff member or deactivating a
venue removes them from future candidates without touching past
entries. **Admin-triggered only** (a "Generate N entries for
YYYY-MM" button) — nothing in this app writes a financial record on a
timer. Idempotent by construction: `CostEntry` gained
`recurring_source_type` / `recurring_source_id` / `recurring_period`
columns with a DB-level unique constraint, so Generate is always safe
to click again — an already-generated venue/staff for that period is
skipped, never duplicated. Editing a venue's rent or a staff member's
salary only affects **future** months; an already-generated entry is a
real historical record, editable individually like any other entry via
`/costs/{id}/edit`, never silently rewritten.

**18% GST on rent (2026-08-30 addition, per explicit request)** —
`Venue.monthly_rent` stays the pre-GST base figure an admin
negotiates/edits; `services/recurring_costs.py`'s `RENT_GST_RATE`
(18%) is added on top at candidate-computation time, so the Cost
Management preview table and the actual generated `CostEntry.amount`
always show the same GST-inclusive total (never two different numbers
for the same candidate). Salaries are never subject to GST. The
Venues page (`/venues`) shows both the base rent and the GST-inclusive
total for admin clarity; the generated entry's `item_name` notes
"(Rent incl. 18% GST)" so it's visible directly in the Raw Entries
ledger without a schema change.

Migration note for an **existing** deployment (same "no Alembic"
situation as `venue_provider`/`is_super_admin`/`email` above): `Venue`
is a brand-new table (free via `create_all()`), but `Staff.monthly_salary`
and `CostEntry`'s three `recurring_*` columns are new columns on
existing live tables and need:
```sql
ALTER TABLE staff ADD COLUMN monthly_salary NUMERIC(12, 2);
ALTER TABLE cost_entry ADD COLUMN recurring_source_type VARCHAR(32);
ALTER TABLE cost_entry ADD COLUMN recurring_source_id INTEGER;
ALTER TABLE cost_entry ADD COLUMN recurring_period VARCHAR(7);
ALTER TABLE cost_entry ADD CONSTRAINT uq_cost_entry_recurring
  UNIQUE (recurring_source_type, recurring_source_id, recurring_period);
```
A fresh install gets all of this for free via `create_all()`.

## PayU Reconciliation (2026-08-30, admin-only, `/reconciliation`)

Matches each machine-recorded UPI order against PayU's own transaction
records for a chosen date range — a separate endpoint from Cost
Management/Order Summary, per explicit request.

**How the match works.** Confirmed live (2026-08-30) before writing any
code: every raw order row from the target application embeds a
`clients` object exposing PayU-shaped integration fields
(`upi_key`, `upi_url1/2/3` pointing at a `/merchant/postservice`-shaped
endpoint) — the target's own backend proxies a PayU-compatible gateway.
`out_trade_no` (consistently populated; `trade_no` was empty on every
order checked, including a successful one, so it's not usable) is the
merchant-supplied reference sent to that gateway — confirmed with the
account owner that this is the same "external order id" PayU's own
transaction records reference alongside PayU's own internal id
(`mihpayid`). `services/reconciliation.py` matches on
`OrderPaymentRecord.out_trade_no == PayUTransaction.txnid`.

Only `pay_type == "UPI"` orders are PayU-eligible — the only other
value ever seen, `"Self-check repair"`, is a free/maintenance category
PayU never sees, so it's excluded from matching entirely rather than
appearing as a false "missing" row. Four outcomes: **Matched**, **Amount
Mismatch** (same reference, different amount), **Missing in PayU** (the
machine recorded a payment but PayU has no record — the most serious
case), **Missing on Machine Server** (a successful PayU transaction
with no corresponding order).

**New `OrderPaymentRecord` table** (individual per-order rows, unlike
`OrderSummary`'s daily aggregates) — needed because reconciliation has
to match one specific sale to one specific gateway transaction, which a
grouped-by-(date, device_app, price, pay_type) row can't do. Populated
going forward by `orders/realtime_worker.py` and `orders/backfill.py`,
right alongside (not instead of) the existing `OrderSummary`
aggregation — same raw fetch, two persistence paths. New table, no
manual migration needed (`create_all()` handles it on any existing
deployment).

**`services/payu_client.py`** implements PayU's own "Get Transaction
Details" API (`POST .../merchant/postservice.php?form=2`,
`command=get_Transaction_Details`, SHA512 hash of
`key|command|start_date|salt`) — confirmed against PayU's official docs
(docs.payu.in) before implementing, including one correction their own
docs' prose got wrong in a spot-check: the response key is
`Transaction_details` (capital T) and it's a JSON **array**, not the
dict-keyed-by-txnid shape a first read suggested.

**Credentials**: add `PAYU_MERCHANT_KEY` and `PAYU_MERCHANT_SALT` to
`.env` (from your [PayU dashboard](https://payu.in/business/transactions)
— account/API settings) — see `.env.example`. `PAYU_ENV=test` switches
to PayU's sandbox base URL. Until both are set, `/reconciliation` shows
a setup message instead of failing confusingly. The PayU call only
fires when an admin explicitly clicks "Run Reconciliation" (`?run=1`)
— never on page load — same "nothing calls a paid external API
silently" rule every other admin-triggered action in this app follows.

## Senior Management Report (2026-08-30, admin-only, `/reports/management`)

An aggregated + monthly breakdown of sales/revenue/cost/profit, venue
performance ranked by sales volume, and per-machine downtime by time of
day, downloadable as a PDF — `services/management_report.py` computes
it, `services/report_pdf.py`/`services/chart_svg.py` render it, gated
behind the existing `require_admin` dependency (no new role).

**Generation is a background job, not an on-screen dashboard (updated
2026-08-30, per explicit request).** `/reports/management` is a control
panel: pick a period + optional machine, click Generate, and a
`ReportJob` row (`db/models.py`, `PENDING` → `RUNNING` →
`SUCCESS`/`FAILED`) appears in the report history below, with the page
auto-refreshing (the same `<meta http-equiv="refresh">` idiom
`equipment_detail.html` already used) only while something is actually
in flight. The web process (`backend/api/reports.py`) only ever
creates that row and later reads a finished one back — it never
computes a report or launches a browser itself. All of that —
`services/management_report.py`'s computation and the real headless-
Chromium PDF render (`services/report_pdf.py`) — runs in
`services/report_job_worker.py`, a third loop in the existing
background worker process (`monitoring/combined_worker.py`, alongside
equipment polling and order sync) that polls for `PENDING` jobs every
10 seconds. This is "an isolated report generation process" per
explicit request: report generation can never block or crash a web
request, and a single job's failure (recorded as `FAILED` with an
error message) can never take equipment monitoring or order sync down
with it. Finished PDFs are stored directly as bytes in Postgres
(`ReportJob.pdf_data`) — this app has no other on-disk file-storage
convention, and a report PDF is tens of KB, trivial at this scale.

**Cost/profit is company-wide only.** `CostEntry` has no per-venue or
per-machine link at all (Rent ties to a `Venue`, but every other
category — Oranges, Glass, Straws, Salaries, etc. — ties to nothing) so
there's no honest way to attribute cost, and therefore profit, to one
machine or venue without fabricating an allocation. Venue performance
is ranked by **sales volume only** (explicit requirement: "based on
sales number") — this sidesteps the gap entirely. Scoping the report to
one machine (the "Machine" dropdown, or `?equipment_id=`) shows that
machine's own sales and downtime but explicitly omits cost/profit and
venue ranking, with a note explaining why, rather than silently
attaching a company-wide number to one machine.

**"Outperforming"/"Underperforming"** venues are relative to the
average orders-per-venue across all venues in the selected period, not
a fixed target — documented directly in the report for transparency.
The venue table also shows each venue's **revenue** alongside its order
count (2026-08-30 addition, per explicit request) — shown for
reference only, never driving the Outperforming/Underperforming call
itself, which stays sales-volume-only.

**Charts (2026-08-30)**: a "Sales Over Time" line chart and a "Sales by
Venue" bar chart, both plain server-rendered inline SVG
(`services/chart_svg.py`) rather than Chart.js/canvas — the same markup
has to work unmodified inside the static PDF export, where nothing
guarantees a client-side chart library finishes drawing to a `<canvas>`
before Playwright's snapshot, and no CDN access should be required to
produce a report at all. Per explicit request, the "Sales Over Time"
chart's granularity follows the selected period: **weekly or monthly**
periods chart by **exact date** (`ManagementReport.daily`, always
computed alongside the monthly breakdown); every other period (daily,
YTD, custom) charts by **month** instead — `backend/api/reports.py`
picks which one feeds the chart; `services/management_report.py` stays
unaware of that presentation choice. Single-hue, single-series charts
(no legend needed) following the project's data-viz method: `<=24px`
columns with a 4px rounded cap, a 2px round-cap line, hairline
gridlines, y-axis ticks rounded to clean numbers, and direct value
labels (line: the endpoint; bars: every cap, since there are only ever
a handful of venues) rather than a number on every point. X-axis labels
thin themselves out (show every Nth one) rather than overlap when a
month's worth of daily points would otherwise collide.

**Downtime, backfilled with real historical depth (2026-08-30)**:
downtime is sourced from `FaultLogHistory` (`db/models.py`), backfilled
from the target application's own historical Fault Information log
(`monitoring/fault_log_backfill.py` — run `python -m
monitoring.fault_log_backfill` any time; safe to re-run, a unique
constraint on `(equipment_id, target_log_id)` makes it idempotent) —
**not** `FaultIncident`, which only has data from whenever this app's
own polling started running. The target's own log has real history per
machine (435 rows across the 6 tracked machines as of this writing,
some going back to January 2026), so the report's downtime section now
has the same historical depth as its sales/revenue figures instead of
reading zero for any date before this app existed.

Only rows where `is_stop=True` (the target's own "Record" column) count
as downtime — a component fault that never actually stopped the machine
isn't downtime. Overlapping fault intervals on the same machine (two
components failing at once) are merged into their union before summing,
so simultaneous faults never double-count. Each interval (clipped to
the selected period, same clipping idiom as `staff/leave_summary.py`'s
month-boundary handling; a still-uncleared fault counts through "now")
is then split across Morning (06:00–12:00) / Afternoon (12:00–17:00) /
Evening (17:00–21:00) / Night (21:00–06:00) in **IST**, this app's
established reporting timezone — an interval spanning multiple days or
crossing midnight is walked day-by-day and correctly merged into one
Night total rather than reported as two separate fragments.

**Worked example**: a real fault on Gravity ran `2026-08-28 13:02:44`
to `14:02:05` IST (59m 21s). It's fully inside Afternoon, so it
contributes `59m` to that machine's Afternoon column and `0m` to the
other three for that period. An interval that actually crosses a
boundary (say `11:50`–`12:10`) would split proportionally: 10 minutes
to Morning, 10 to Afternoon.

**PDF export** uses Playwright's Chromium — already a hard dependency
of this project (`monitoring/browser_manager.py`) with the browser
already downloaded and verified working in this environment — rather
than adding a new PDF library (WeasyPrint needs GTK/Pango native
libraries that are painful to install on Windows; reportlab/fpdf2 would
mean hand-laying-out tables instead of reusing the existing HTML/CSS).
A throwaway headless Chromium instance (launched by the worker, per
job, and closed immediately after) renders a self-contained HTML
document (`management_report_pdf.html` — inlined CSS, since
`page.set_content()` has no live server context for `/static` to
resolve against) built from `management_report_content.html`, the sole
place the report's actual markup lives now that there's no on-screen
preview to keep in sync with it. This is completely independent of
`monitoring/browser_manager.py`'s persistent jwintell.com session
browser — a fresh instance per job, never touching the saved
target-application session.

**Date-wise Sales toggle (2026-09-21)**, per explicit request — "add
datewise sales data toggle button" for viewing daily sales within the
current month. The day-by-day breakdown (`report.daily`) was already
computed unconditionally by `services/management_report.py` (it feeds
the "Sales Over Time" chart for weekly/monthly periods) but never
rendered as a table; the generate-report form now has an "Include
date-wise sales breakdown" checkbox that adds a **Date-wise Sales**
table (date/orders/revenue, one row per day in the report's range) to
the PDF only when checked, keeping the default (aggregated/monthly)
report concise. `ReportJob.include_datewise_sales` records the choice
per job (shown as a Yes/— column in Report History) and flows through
unchanged to `render_management_report_pdf()`.

Migration note for an **existing** deployment (same "no Alembic"
situation as elsewhere): `include_datewise_sales` is a new column on
the existing live `report_job` table —
```sql
ALTER TABLE report_job ADD COLUMN include_datewise_sales BOOLEAN NOT NULL DEFAULT FALSE;
```
A fresh install gets this for free via `create_all()`.

## Fault Information detail in alerts (2026-08-29)

A malfunction alert's coarse `fault_type` text (e.g. "Fault") never told
you *which part* of the machine actually failed. `monitoring/worker.py`
now enriches every newly-opened or escalated `FaultIncident` with the
real per-component detail from the target application's own **Equipment
Management → Fault Information** tab (`device/device_fault_log` —
confirmed live 2026-08-29 against a real historical row: target id
21529, device 109, code `luozhentanzhenkaiguan`), stored as
`FaultLogEntry` rows (new table — safe on any existing deployment, no
manual migration needed, see the no-Alembic note above). This is
deliberately best-effort: `monitoring/worker.py`'s
`_attach_fault_log_detail()` runs AFTER the incident is already
persisted and swallows any failure — a target hiccup at that exact
moment degrades to "no extra detail this time," never blocks incident
detection or suppresses the alert email itself.

The raw `code` field is a pinyin slug (e.g. `dianzicheng`) — translated
to English (e.g. "Electronic scale Malfunction") via
`monitoring/fault_codes.py`'s static table, harvested verbatim from the
target's own backend language pack
(`GET /ajax/lang?controllername=device.device_fault_log&lang=en-us` —
the same endpoint its own UI uses to render that tab, confirmed to
reproduce "Electronic scale Malfunction" / "Drop cup probe switch
Malfunction" exactly). An unrecognized code (this table isn't guaranteed
exhaustive) falls back to a readable guess and logs a warning rather than
erroring.

Surfaced in two places:
- **Email**: `services/alert_engine.py`'s incident/escalation messages
  gain an "Active Faults" section listing only the still-uncleared
  (`is_clean=False`) entries — an entry the target already auto-cleared
  by the time the email sends isn't an active problem any more.
- **UI**: `/faults/{incident_id}`'s new "Fault Information" table shows
  every attached entry (cleared or not) with its Record/Clear
  Time/Automatic Clear columns, matching the target's own tab layout.

## Alert Recipients (admin-only, `/alert-recipients`)

The email alerting system itself (`services/alert_engine.py` +
`services/notification_service.py`, wired into `monitoring/worker.py`)
already existed from Phase 4 — StateManager detects a machine entering
MALFUNCTION/OFFLINE exactly once (never once per poll), AlertEngine
composes a message with the equipment's name/ID/code/health/fault
text/severity/detected time, and NotificationService delivers it (real
SMTP if configured, a console log otherwise). What's new (2026-08-24) is
**a second recipient source**: `AlertRecipient`, a plain admin-managed
email list that doesn't require a dashboard `User` account — for people
who need malfunction alerts but should never need to log into this app.
It's additive, not a replacement: admins-always and per-user
`/subscriptions` still work exactly as before; `AlertEngine.get_recipients()`
now unions in every active `AlertRecipient` email whenever severity is
Critical (same trigger as the admin-always rule), de-duplicated with
everyone else before sending.

**Update 2026-08-26: real SMTP is now configured and verified** — a live
send to `support@refresha.in` succeeded (confirmed via the
`services.notification_service: Email sent to ...` log line, which only
appears after `smtplib` completes a real connect/login/send, not the
`[console-channel — no SMTP configured]` fallback). The email-alerting
system was fully built and wired from Phase 4 onward; this closed the
one remaining real gap.

**Update 2026-09-06: SMTP doesn't work in production — switched to
Resend's HTTPS API.** A real Critical-severity malfunction alert
(Gravity) silently failed to send on Railway. Diagnosed live, in order:
recipients resolved correctly (ruling out a recipient-logic bug); an
IPv4-only DNS fix (`EmailNotificationChannel` was resolving
`smtp.gmail.com`'s IPv6 address first, which Railway's containers can't
route — that fix is still correct and stays in place) resolved one real
bug, but the send still failed with a connection timeout; a direct port
test from inside the `combined-worker` container
(`timeout 5 bash -c '</dev/tcp/smtp.gmail.com/<port>'` for 587, 465,
and 25) confirmed Railway blocks outbound SMTP entirely — a common
PaaS anti-spam-relay policy, not fixable at the application layer.
`services/notification_service.py` gained `ResendEmailChannel`, which
sends over HTTPS (never blocked) instead of raw SMTP — it's selected
automatically whenever `RESEND_API_KEY`+`RESEND_FROM_EMAIL` are set,
taking priority over SMTP (see `NotificationService.__init__`). Local
dev is unaffected — SMTP isn't blocked on a home/office network, so it
keeps working exactly as before when Resend isn't configured. See
`.env.example` for the two new variables.

## Critical Faults Digest (`services/fault_digest.py`)

A second, distinct kind of email from the per-incident instant alerts
above: **one email listing every machine currently in a Critical
(Malfunction/Offline) active incident**, always as a table with fault
details — not "as soon as detected" (that's the instant alert's job),
but "what's wrong right now, all in one place." Per explicit request,
the table is genuinely tabular in a real inbox: `NotificationService`
gained an optional `html_body` (multipart/alternative — the plain-text
table is the fallback for a client that can't render HTML), and
`services/fault_digest.py` builds both from the same row data so they
can't drift apart. Columns: Machine, Equipment ID, Equipment Code,
Fault, Health, Since (IST), Duration.

Recipients reuse `AlertEngine.get_recipients(session, equipment_id=None,
severity="Critical")` — the same admins-always + `AlertRecipient`-always
+ subscribed-to-all-machines audience every other Critical notification
already uses, not a second recipient system. Sends nothing (and says so)
when no machine is currently Critical — an empty "all clear" digest
would just train people to ignore these emails.

```bash
python -m services.send_fault_digest
```

Live-verified 2026-08-26 against the 3 real active OFFLINE incidents
(Warehouse, REFRESHA 2, REFRESH-1): sent successfully to
`demo@example.com`/`support@refresha.in`, with a rendered HTML table
confirmed by screenshot.

## Dashboard display: IST timestamps, no demo data

Two small but real fixes (2026-08-26): the two synthetic
`[DEMO]`-labeled equipment rows (`[DEMO] Faulty Unit`,
`[DEMO] Low Stock Unit` — added earlier for screenshot/badge coverage,
never part of the schema or a seed script) were deleted from the demo
database; the Fleet Overview now shows only the 6 real machines.

Every timestamp on the equipment monitoring pages (Last Poll, Next
Poll, Last Updated, incident Detected/Resolved, snapshot history) was
being rendered as a raw Python `datetime` — technically correct but
silently in UTC, since that's what `db/models.py`'s `_utcnow()` stores.
Per explicit request, these now display in **IST**, via a new `ist`
Jinja filter (`backend/templating.py`) that reuses the same `IST_TZ`
already established for Order Summary (`orders/mapping.py`) rather than
defining a second timezone constant — one filter, applied at every
render site in `dashboard.html`/`equipment_detail.html`/`faults.html`/
`fault_detail.html`. The underlying stored values are unchanged (still
UTC in the DB, and the JSON `/api/monitoring/status` endpoint still
reports UTC ISO timestamps for programmatic consumers) — this is a
display-layer change only.

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
  alert_engine.py             who gets notified (admins + AlertRecipient
                             flat list + subscriptions, §19) and what the
                             message says (§18) for the incident events
                             StateManager already found

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
    orders.py                    Order Summary tab — period/breakdown selection + chart
  templates/, static/          Jinja2 HTML + one stylesheet, no build step

orders/                        Order-summary feature (independent of
                             equipment monitoring — see its own README section)
  selectors.py                  confirmed order-list API + the UTC+8 finding
  mapping.py                    raw row -> OrderRecord (order_date computed in IST_TZ)
  client.py                     httpx fetch for one IST day (queries 2 of the
                             target's UTC+8 days, filters to the IST match), paginated
  summary.py                    filter + (machine, price, pay_type) grouping
  store.py                       DB persistence (OrderSummary/OrderSummaryRun);
                             "already processed" detection, idempotent re-save
  rollup.py                      query-time aggregation into any requested view
                             (used by both the CLI printout and the UI)
  backfill.py                   CLI: sequential day-by-day (IST), skips processed dates
  realtime_worker.py            keeps TODAY (IST) continuously up to date -- the
                             deliberate exception to backfill.py's "only ever
                             process a fully-elapsed day" rule

tests/
  conftest.py                  shared fixtures (in-memory SQLite + record factory)
  test_health_engine.py        unit tests for the rule table
  test_state_manager.py        incident dedup/escalation/resolution — reproduces
                             the spec's own "one alert, not four" worked example
  test_notification_service.py SMTP interaction mocked; credentials-never-logged check
  test_alert_engine.py         recipient-resolution matrix (admin/AlertRecipient/
                             subscription/severity/equipment-scoping/dedup)
  test_alert_recipients_backend.py  /alert-recipients CRUD + RBAC
  test_lightweight_client.py   cookie loading, redirect-to-login detection,
                             pagination, error classification — httpx.MockTransport,
                             no real network
  test_backend.py              FastAPI TestClient: auth, RBAC, all pages, subscription CRUD
  test_orders_mapping.py       field mapping + the IST day-boundary calculation
  test_orders_client.py         fetch_day's two-target-day IST reconciliation
  test_orders_summary.py       filter + (machine,price,pay_type) grouping, incl. the
                             mid-day-price-change requirement, hand-computed values
  test_orders_rollup.py        aggregation math (weighted averages across combined groups)
  test_orders_store.py         DB round-trip, idempotent re-save, quiet-day handling
  test_orders_backend.py       Order Summary tab: auth, period switching, breakdowns
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

- ~~🔖 Bookmarked: host on an always-on cloud environment~~ — **in
  progress as of 2026-08-25**, see "Deploying to the cloud" below.
- Close the live-SMTP verification gap once real credentials are set
  (see "Known gap" above and the Alert Recipients section) — separate
  from cloud hosting, in progress independently.
- Phase 6 (partially addressed by the cloud deployment below — Docker
  images now exist, live Postgres will be closed once actually
  deployed): Alembic migrations, structured logging, retry-with-backoff
  within a poll cycle (spec §26), browser crash recovery, secrets
  manager, DB backups, rate limiting.
- Optional: swap the dashboard for a Next.js frontend against the
  existing `backend/api/` routes, if still wanted after seeing the
  server-rendered version.
- Wire the API's rich `this_fault` per-component detail into incident
  records once the health-rule vocabulary is confirmed against a real
  fault.

## Deploying to the cloud (Railway)

The app runs as **two** deployed services sharing one Docker image
(`Dockerfile`, repo root) plus a managed Postgres — not three, even
though there are three local `--loop` scripts (`monitoring/worker.py`,
`orders/realtime_worker.py`, `uvicorn`). Why: only `monitoring.worker`
ever re-authenticates via Playwright and writes a fresh session to
`STORAGE_STATE_PATH`; `orders/client.py`'s `OrdersClient` reads that same
file. Hosting platforms attach a persistent volume to exactly one
service each, so running the two workers as separate services would
leave `orders.realtime_worker` with no way to ever see a fresh session
after the first one. **`monitoring/combined_worker.py`** is the fix —
it runs both existing loops (`monitoring.worker`'s `run_forever()` and
`orders.realtime_worker`'s `run_loop()`, both completely unmodified —
this is a thin `asyncio.gather()` wrapper) in one process, so they share
one container filesystem and therefore one volume. The other half of
the fix is `OrdersClient.reload_cookies()` (new) — without it, being in
the same process wouldn't be enough on its own, since this client
previously only ever loaded the session file once, at construction.
Both scripts still work completely unchanged standalone for local dev
(`python -m monitoring.worker --loop` / `python -m orders.realtime_worker --loop`).

**Deployed services**: `web` (`uvicorn backend.main:app`, public,
`/healthz` for the platform's health check) and `combined-worker`
(`python -m monitoring.combined_worker --loop`, no public port, one
persistent volume mounted at `/app/data` for the session file) — plus a
managed Postgres addon. `Dockerfile` uses
`mcr.microsoft.com/playwright/python:v1.47.0-jammy` as its base image
(version-matched to `requirements.txt`'s `playwright==1.47.0` pin) so
headless Chromium is already present for `combined-worker`'s rare
re-auth events — `web` never touches Playwright at all (spec §30).

Full step-by-step walkthrough (account creation through first deploy
through verification, written for a first-time cloud deployer):
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

**Prefer full root-level control over a managed platform?** See
[`docs/DEPLOYMENT_VPS.md`](docs/DEPLOYMENT_VPS.md) for a self-managed
VPS path instead (verified against Hostinger KVM specifically) — same
Dockerfile and `combined_worker.py`, expressed as three Docker Compose
services with your own Nginx/TLS/firewall, instead of a managed
platform's services + plugin.

**Explicitly deferred for this first deployment** (per "continue
building the remaining features later"): Alembic migrations (schema
still created via `Base.metadata.create_all()`, run once by the
`db.seed_admin`/`db.seed_venue_mapping` one-off commands after first
deploy), rate limiting, CSRF protection, structured logging/observability,
automated DB backups, a custom domain.

**Not free** — two always-on processes plus managed Postgres is
continuous compute. Realistic range on Railway for this workload:
**~$10–20/mo**, moving with actual usage.
