# Changelog

All notable milestones for this project, newest first. Not every commit
gets an entry — this tracks meaningful, user-facing checkpoints, not a
mirror of `git log`. See [`docs/prompt_logs.md`](docs/prompt_logs.md) for
the verbatim request behind each of these, and
[`docs/NEXT_SESSION_PROMPT.md`](docs/NEXT_SESSION_PROMPT.md) for the
living, more granular version of "pending work."

## [Unreleased] — since 3.0.0

**Custom date range for the on-screen Order Summary view** (2026-09-22),
per explicit request. The "Download a Sales Report" form already had
a real Start/End date picker for "Custom range"; the on-screen filter
above it (shared by admin and vendor) only offered Daily/Weekly/
Monthly/YTD with a single anchor date — the backend route already
accepted `period=custom&start=&end=` (same `period_range()` used
everywhere else), so this was a template-only gap. Period `<select>`
now includes "Custom range" with Start/End fields shown via the same
toggle idiom as the report forms, distinct element IDs to avoid
clashing with the vendor-report form on the same page.

**Vendor-downloadable sales report** (2026-09-22), per explicit
request. Venue partners can now generate/download their own PDF sales
report from `/orders/summary`, reusing the Senior Management Report's
`ReportJob` background-worker infrastructure but rendering a separate,
minimal `VendorSalesReport` object with no revenue/cost/profit field
at all (not just hidden in the template) — glasses-sold only, scoped
to their own venue's machine(s). Always shows a Monthly Breakdown
table (naturally the right shape for a YTD request); the same
"Include date-wise sales breakdown" checkbox as the admin report adds
a day-by-day table too. Download route enforces per-vendor ownership.
Needs `ALTER TABLE report_job ADD COLUMN venue_provider VARCHAR(255);`
on an existing deployment — see README's "Vendor (venue_partner) view"
section.

**Auto-sync a Venue to a same-named machine** (2026-09-22), per
explicit request. New `services/venue_machine_sync.py` auto-creates a
`VenueMapping` row whenever a venue's name exactly matches exactly one
`Equipment` name (e.g. venue "Navi" -> machine "Navi") — no more manual
`db.seed_venue_mapping` step for the common case. `GET /venues` runs
this for every venue on the page load (self-healing — an existing gap
fixes itself next time the page opens), and create/edit-venue also run
it inline. `/venues` gained a "Mapped Machine(s)" column so the
previously-invisible mapping state is visible directly on the page.
Never guesses: zero/multiple name matches, or a machine already mapped
elsewhere, are left untouched — a differently-named venue/machine pair
(e.g. "PNR Felicity" / "PNR") still needs the manual command.

**Fix: venues added at `/venues` missing from the Add/Edit User venue
dropdown** (2026-09-22) — real reported bug. The dropdown
(`backend/api/users.py::_venues()`) only ever read `VenueMapping`
(the machine-scoping table), a completely separate table from `Venue`
(the master list `/venues` manages) that only agree by convention, not
a hard FK — a venue added at `/venues` with no machine mapped to it
yet was invisible here. Now returns the union of both, so newly-added
venues are selectable immediately.

**Senior Management Report: date-wise sales toggle** (2026-09-21), per
explicit request. The generate-report form gained an "Include
date-wise sales breakdown" checkbox — when checked, the PDF gets a new
Date-wise Sales table (one row per day) alongside the existing Monthly
Breakdown; off by default. The underlying daily data
(`report.daily`) was already computed, just never shown as a table.
Needs `ALTER TABLE report_job ADD COLUMN include_datewise_sales BOOLEAN NOT NULL DEFAULT FALSE;`
on an existing deployment — see README's "Senior Management Report"
section.

**Editable/deletable staff leaves + half-day leave** (2026-09-10), per
explicit request. Every Recent Leaves row gained Edit/Delete links
(`/staff/leaves/{id}/edit`, `/staff/leaves/{id}/delete`), matching the
roster's existing pattern. New `StaffLeave.is_half_day` column — a
half-day leave is always a single day, counted as 0.5 days in
`staff/leave_summary.py`'s month totals instead of 1. Needs
`ALTER TABLE staff_leave ADD COLUMN is_half_day BOOLEAN NOT NULL DEFAULT FALSE;`
on an existing deployment — see README's "Staff & Leave Management"
section.

**Vendor Order Summary trimmed to glasses-sold only** (2026-09-10), per
explicit request — venue partners now see exactly one figure (the
glasses-sold count, i.e. `number_of_orders`) instead of Avg Price/Total
Oranges/Avg Juice Weight alongside it (Revenue/Oranges-per-Glass were
already admin-only). Admin's own view is unchanged.

**Alert emails switched from SMTP to Resend's HTTPS API** (2026-09-06),
after a real Critical-severity malfunction alert (Gravity) silently
failed to send in production. Diagnosed live, in order: recipients were
resolving correctly, an IPv4-DNS fix (still kept, still correct)
resolved one real bug but the send still failed, and a direct port
test from inside the container (`timeout 5 bash -c
'</dev/tcp/smtp.gmail.com/<port>'`) confirmed Railway blocks outbound
SMTP entirely — ports 587, 465, and 25 all blocked, a common PaaS
anti-spam-relay policy with no code-level fix. New
`services/notification_service.py::ResendEmailChannel` sends over
HTTPS instead, which isn't blocked; it's selected automatically
whenever `RESEND_API_KEY`+`RESEND_FROM_EMAIL` are set, taking priority
over SMTP — local dev keeps working unchanged via SMTP, since outbound
SMTP isn't blocked on a home/office network. See `.env.example` for
setup.

**PayU reconciliation** (2026-08-30, admin-only, `/reconciliation`),
per explicit request. Matches each machine-recorded UPI order against
PayU's own transaction records via `out_trade_no` <-> `txnid` (confirmed
with the account owner as the correct correlation key after inspecting
real order data — the target's own per-order records expose PayU-shaped
integration fields showing its backend proxies a PayU-compatible
gateway). New `OrderPaymentRecord` table stores individual orders (not
just `OrderSummary`'s daily aggregates) going forward. New
`services/payu_client.py` implements PayU's "Get Transaction Details"
API (verified against PayU's own docs, including one correction to
their documented response shape). Add `PAYU_MERCHANT_KEY`/
`PAYU_MERCHANT_SALT` to `.env` to enable — see README's "PayU
Reconciliation" section.

**Downtime backfilled with real historical depth** (2026-08-30), per
explicit request. New `monitoring.fault_log_backfill` (run standalone,
idempotent) pulls each machine's COMPLETE historical Fault Information
log from the target application into a new `FaultLogHistory` table.
The Senior Management Report's "Downtime per Machine" section now reads
from this instead of `FaultIncident` (which only ever had data from
whenever this app's own polling started) — 435 real historical rows
backfilled across the 6 tracked machines, some going back to January
2026. Only `is_stop=True` rows count as downtime; overlapping faults on
the same machine are merged into their union before summing, so
simultaneous component faults never double-count. See README's
"Senior Management Report" section for a worked example of the
downtime calculation.

**Report generation moved to a background job (an "isolated report
generation process")** (2026-08-30), per explicit request. The
Senior Management Report's on-screen dashboard is gone — `/reports/
management` is now a control panel: pick a period/machine, click
Generate, and a `ReportJob` row (PENDING → RUNNING → SUCCESS/FAILED)
shows in a report history table, with a Download PDF link once ready.
Report computation and PDF rendering now run ONLY in
`services/report_job_worker.py`, a third loop in the existing
background worker process (`monitoring/combined_worker.py`) that polls
for pending jobs every 10 seconds — the web process never computes a
report or launches a browser itself, and one job's failure can never
crash a web request or take equipment monitoring/order sync down with
it. Finished PDFs are stored as bytes directly in Postgres.

**18% GST added to recurring rent** (2026-08-30), per explicit
request. A Venue's configured monthly rent stays the pre-GST base
figure; 18% GST is added on top at generation time, so the Cost
Management preview and the actual generated entry always agree.
Salaries are unaffected. The Venues page shows both the base and
GST-inclusive figures for clarity.

**Charts + venue revenue in the Senior Management Report** (2026-08-30),
per explicit request. A "Sales Over Time" line chart and a "Sales by
Venue" bar chart, rendered as plain inline SVG (`services/chart_svg.py`
— no Chart.js, so the same markup renders identically in the PDF
export with no client-side-JS-timing risk). The venue performance table
now shows revenue alongside orders (reference only — ranking stays
sales-volume-only). Per explicit rule: weekly/monthly periods chart the
time series by exact date; every other period charts by month.

**Senior Management Report, downloadable as a PDF** (2026-08-30,
admin-only, `/reports/management`), per explicit request. Aggregated +
monthly sales/revenue/cost/profit, venues ranked "Outperforming" /
"Underperforming" by sales volume (relative to the period's average),
and per-machine downtime broken down by time of day (Morning/Afternoon/
Evening/Night, IST). Optional machine scope (`?equipment_id=`) shows
that machine's sales/downtime only — cost/profit and venue ranking are
company-wide-only by nature (`CostEntry` has no per-machine or per-venue
link) and are explicitly omitted, with a note, rather than faked. PDF
export renders via Playwright's Chromium (already a project dependency)
— no new PDF library added. See README's "Senior Management Report"
section.

**Recurring Costs (rent + salaries), a new Venues admin page**
(2026-08-30), per explicit request. New `/venues` page to onboard/edit
venues and their monthly rent (new `Venue` table); `Staff` gained an
optional `monthly_salary`. Cost Management's new "Recurring Costs"
section derives one Rent entry per active venue and one Staff Salaries
entry per active staff member straight from those two lists — nothing
retyped, and the candidate list updates itself as venues/staff are
onboarded, edited, or offboarded. Admin clicks "Generate" for a chosen
month (defaults to the current one); safe to click more than once —
`CostEntry` gained a unique-constrained `(recurring_source_type,
recurring_source_id, recurring_period)` so a period is never
double-generated. See README's "Recurring Costs" section for the
manual migration note (new columns on `staff` and `cost_entry`).

**Staff email + Alert Recipients quick-add + auto-removal on leaving**
(2026-08-29), per explicit request — `Staff.email` (optional, new
column) lets a staff member be added at `/staff` or `/staff/{id}/edit`
without requiring one. `/alert-recipients` gained a "Quick Add from
Staff" dropdown listing active staff with an email on file, so an admin
can add them as a Critical-alert recipient without retyping the email.
Offboarding a staff member (either the dedicated "Mark as left" action
or setting an end date via the edit form) now auto-deactivates any
`AlertRecipient` row matching their email — a left staff member stops
getting equipment alerts through their staff email automatically,
without an admin having to remember to remove them separately.
Deliberately one-directional: reactivating a staff member does not
auto-restore a previously-deactivated recipient. See README's "Staff
email" section for the manual migration note.

**Super admin reassigned to support@refresha.in** (2026-08-29), per
explicit request — the original seed account, `1206ashish656@gmail.com`,
was deactivated in the same change. `db/seed_admin.py`'s
`SUPER_ADMIN_EMAIL` constant updated to match, so a future re-run
reflects the current designation rather than the old one. See README's
updated "Super admin" section.

**Malfunction alerts now carry real per-component fault detail**
(2026-08-29) — per explicit request, retrieved from the target
application's own Equipment Management → Fault Information tab
(`device/device_fault_log`, confirmed live against a real historical
row) rather than only the coarse `fault_type` string. New `FaultLogEntry`
table (new table, no manual migration needed) attached to a
`FaultIncident` by `monitoring/worker.py`, best-effort so a target
hiccup never blocks incident detection or the alert itself. Raw pinyin
fault codes (e.g. `dianzicheng`) are translated to English (e.g.
"Electronic scale Malfunction") via a static table harvested from the
target's own backend language pack (`monitoring/fault_codes.py`).
Surfaced in the alert email's new "Active Faults" section (still-active
entries only) and in a new "Fault Information" table on
`/faults/{incident_id}` (full history for that incident). See README's
"Fault Information detail in alerts" section.

**Added a self-managed VPS deployment path** (2026-08-28), alongside
the existing Railway one — per explicit request to deploy on the
user's Hostinger account. Researched Hostinger's actual hosting tiers
first rather than assume: confirmed directly from Hostinger's own docs
that shared/cloud hosting has no root access and therefore can't run
Python at all, while **Hostinger KVM VPS** is a real Linux VM (same
category as a DigitalOcean/Linode droplet) that supports everything
this app needs (Python, Postgres, Playwright/headless Chromium,
long-running background processes).

New `docker-compose.prod.yml` (repo root) — three services (`web`,
`worker`, `postgres`) built from the existing `Dockerfile` and reusing
`monitoring/combined_worker.py` unchanged (no application code changes
needed; the Railway deployment already solved the "two loops need to
share one session file" problem this reuses as-is). One genuine
security correction versus the existing dev `docker-compose.yml`:
Postgres (and the web container's own port) bound to `127.0.0.1` only,
not all interfaces — a VPS has a real public IP, so the dev file's
default publish-on-all-interfaces behavior would make Postgres directly
internet-reachable, unlike a laptop behind home-router NAT or Railway's
network-isolated managed Postgres.

New `docs/DEPLOYMENT_VPS.md` — full walkthrough (SSH hardening, `ufw`
firewall, Docker install, DNS, `.env` setup, first build, schema/admin
seeding, Nginx + Certbot TLS, a concrete verification checklist
including confirming Postgres is genuinely unreachable from outside the
VPS) — sibling to `docs/DEPLOYMENT.md`, not a replacement. Explicitly
notes that a VPS's real static IP makes the earlier-explored Cloudflare
Tunnel path unnecessary here — DNS is just one A record
(`equipment.joyjuice.in` → the VPS's IP) in Hostinger's own hPanel,
since `joyjuice.in`/`refresha.in` are already hosted there — without
touching either domain's live website/email records at all.

Built via formal plan mode (1 Explore agent confirming exact current
file contents rather than trusting memory, 1 Plan agent), following the
same disciplined process as the original Railway deployment plan.
Documentation + one new compose file only — cannot be live-verified
from here (no SSH access to the user's VPS); the doc's own Step 10
checklist is written to be concretely checkable by the user themselves.

`README.md` updated with a pointer to the new doc alongside the
existing Railway section.

**Migrated the local dev/demo database from SQLite to real Postgres**
(2026-08-27), fixing the recurring `database is locked` crash at its
actual root rather than mitigating it further. The WAL-mode fix
(previous entry) reduced how often it happened but didn't eliminate
it — watched it recur live during this session, immediately after a
restart, with equipment monitoring going dark again as a direct
consequence of `monitoring/combined_worker.py`'s own deliberate design
("either loop crashing is fatal for the whole process... never silently
keep only half working"). Per explicit user decision: **keep that
crash-coupling design as-is** (still deliberate, still documented) and
instead remove the actual cause — SQLite's single-writer model — by
switching to Postgres, which has proper concurrent-write support.

New `db/migrate_sqlite_to_postgres.py`: copies every row from
`data/demo.db` into the Postgres database configured in `.env`
(`DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`), preserving primary keys,
then resets Postgres's sequences past the migrated max id per table so
new inserts from the running app don't collide. Refuses to run against
a Postgres database that already has any data unless `--force`, since
re-running it isn't idempotent (would insert duplicates) — this is a
one-time carry-over tool, not a sync tool. Tables are copied in
`Base.metadata.sorted_tables` order (topologically sorted by FK
dependency), so referenced tables are always populated before whatever
references them. 4 new tests, using a second SQLite file as a stand-in
Postgres target (this project's tests deliberately never require a real
Postgres instance to run) — the Postgres-only sequence-reset step is
skipped when the target dialect isn't `postgresql`, keeping the function
itself fully testable. 306/306 passing overall.

Live-verified end to end, not just migrated and hoped: created the
`equipment_monitor` role/database (user's own local Postgres, their
existing install), verified real connectivity and schema creation
(`db.init_db`, all 16 tables), migrated all 19,252 existing rows
(equipment history, order summaries, inventory, users, fault incidents,
alert recipients — everything accumulated over this session's testing,
not thrown away), confirmed a fresh insert doesn't collide with a
migrated id (sequence reset works), restarted both processes with
*no* `DATABASE_URL` override (so `Settings.database_url` naturally
composes the Postgres URL from `.env`'s `DB_*` fields), watched 10
consecutive poll cycles complete cleanly with zero lock errors (versus
the crash that had just recurred minutes earlier under SQLite), and
confirmed the demo admin login and full dashboard render correctly
against the migrated data via a real screenshot.

`README.md`'s SQLite quick-start section and `docs/OPERATIONS_RUNBOOK.md`
updated: the runbook's commands no longer carry a `DATABASE_URL=sqlite:
///...` override (that would now point at the wrong, stale database),
and the SQLite path in README is kept as a genuinely still-valid
*quicker* option for a fresh setup, explicitly flagged with the same
concurrency caveat rather than silently left looking equivalent to
the now-primary Postgres path.

**Fixed: Order Summary going stale whenever the app restarts across a
day boundary.** User reported Order Summary wasn't updating; root cause
found directly, not assumed: `orders/realtime_worker.py`'s day-rollover
handling (`run_cycle`'s "one final refresh of the day that just ended")
only closes a gap that happens *while the process is running* — it
tracks `last_seen_date` in memory across its own loop iterations. A
freshly started process has no such memory, so if the app is down when
the IST calendar date rolls over, the day it was last actively updating
never gets its closing refresh and stays frozen on a partial snapshot —
confirmed live: `2026-08-26` was frozen at 326 raw orders from 18:01
UTC, the exact moment the app went down, with `orders/backfill.py`'s
normal caching unable to help either (already `SUCCESS` reads as
"already done").

Fix: new `reconcile_after_downtime()`, run once at startup before the
continuous loop begins (`run_once`/`run_loop`, both `orders.
realtime_worker` standalone and via `monitoring.combined_worker`).
Compares the last date `OrderSummaryRun` actually has against today
(IST): if they match or there's no history yet, nothing to do; if
downtime crossed a day boundary, that last-active day gets force-
refreshed directly, and any fully-elapsed days in between that were
never touched at all get handed to the existing `orders.backfill`
machinery (normal skip-if-cached/fetch-if-missing semantics) — so a
short restart self-heals in one extra request, and a multi-day outage
self-heals in one pass covering the whole gap, automatically, with no
manual command needed.

5 new tests (first-ever-run no-op, same-day-restart no-op, the core
overnight-gap force-refresh, a multi-day gap correctly split between
force-refresh + backfill, and the exactly-one-day-gap boundary case).
302/302 passing overall. Live-verified against the real demo DB: found
`2026-08-26` still frozen at 326 orders from before an earlier restart
in this session — the automatic fix couldn't retroactively catch this
*specific* gap (this morning's restart, which predates the fix, had
already advanced the DB's "last known date" to today before the fix
was ever loaded, so the precondition it checks no longer held) — closed
it with one manual `orders.backfill --date 2026-08-26 --force` (326 →
333 orders, matching the target's real count), confirmed via a fresh
app restart afterward that the reconciliation step runs cleanly with
nothing further to do. Going forward, any future restart across a day
boundary self-heals without that manual step.

**Inventory Management** (new, `/inventory`, admin OR operations — the
same role pair `require_operations` already gates equipment monitoring
with) — per explicit request: track 10 fixed consumables (Oranges,
Sealing Films, Glasses, Straws, Kitchen Cleaner, Dustbin Bags, Floor
Cleaner, Orange Refill Bags, Shower Caps, Gloves), a "Log Usage Today"
form that decrements stock, a "Set Stock" action for corrections/
restocks (no separate restock table — chosen via AskUserQuestion over a
full two-way ledger), and a low-stock warning both emailed and shown as
a dashboard badge (also chosen via AskUserQuestion).

Architecturally closer to `EquipmentCurrentState` (a fixed set of
known items, each holding current state) than to Cost Management's
open-ended ledger — deliberately does NOT reuse
`backend/period_utils.py`/`costs/rollup.py`. Two new tables:
`InventoryItem` (current stock always in the item's base unit — pieces
for Glasses/Straws, with `pieces_per_carton` as a pure conversion aid
for the form/display, never branched on at the persistence layer) and
`InventoryLogEntry` (one audit-trail table with an `entry_type`
discriminator for both usage and correction entries, immutable once
created). Thresholds are configurable, not hardcoded — new
`config/inventory_rules.yaml` + `services/inventory_rules.py`, mirroring
`config/health_rules.yaml`/`HealthEngine`'s exact loading pattern.

The email fires only on the CROSSING (mirrors
`services/state_manager.py`'s `is_fault and not was_fault`) — new
`services/inventory_alert.py`, reusing the same admin + `AlertRecipient`
audience equipment alerts already use, via `NotificationService`
directly (not `AlertEngine.get_recipients()`, which is signature-coupled
to `equipment_id`/`AlertSubscription`). The dashboard badge needs no
persisted flag — recomputed live from `current_stock` vs threshold on
every render, so it always reflects reality and clears itself the
instant a correction restocks above threshold.

New seed script `db/seed_inventory_items.py` (idempotent, never resets
`current_stock` on re-run — only sets it on first creation). Four items
(Kitchen Cleaner, Floor Cleaner, Shower Caps, Gloves) had no unit
specified in the request — defaulted to "units", flagged in both the
seed script and `config/inventory_rules.yaml` for visibility.

Built via formal plan mode (2 Explore agents + 1 Plan agent, 2
clarifying AskUserQuestion calls on the two genuine design decisions
above) given the feature's size (2 new tables, a new config file, a new
RBAC-gated route module, a new alert-service module, a seed script).

38 new tests (`test_inventory_rules.py`, `test_inventory_alert.py` —
including the core "still-below fires zero additional emails"
requirement, `test_inventory_backend.py`, `test_seed_inventory_items.py`).
297/297 passing overall. Live-verified against the real demo DB: seeded
the 10 items, restarted both processes, confirmed the dashboard renders
correctly via screenshot — and, live during this same verification
window, the account owner independently exercised the feature for real
through their own browser session (`support@refresha.in`), setting real
stock levels including a Glasses correction (8500 → 5) that crosses
below its configured threshold (480) — confirmed via the DB directly
(`inventory_log_entry`) and the web process's access log (no errors),
though actual email inbox delivery wasn't independently confirmed since
that requires the account owner's own inbox access.

**docs/LOCAL_SHARING.md** (new) — how to expose the locally-running app
to one specific external person without cloud deployment, per explicit
request ("expose this application to a friend... securely... restrict
the access to this application only"). Cloudflare Tunnel (`cloudflared`)
forwards only port 8123 to a chosen hostname — no other port/service on
the machine is reachable, no router/firewall changes needed (outbound-only
connection). Cloudflare Access (email allow-list) gates the tunnel itself
— anyone not on the list is stopped at Cloudflare's edge before ever
reaching the app; the app's own login (existing) is the second,
independent layer once past that gate. Verified no code changes were
needed first: checked `backend/api/auth.py`'s session cookie doesn't
hardcode a domain, so it works correctly under a different external
hostname without modification. Documentation only — the actual setup
requires the user's own Cloudflare account/domain and wasn't run here.

**docs/OPERATIONS_RUNBOOK.md** (new) — a copy-pasteable command
reference for the application management team: restarting the server,
backfilling order data, adding/removing test equipment, triggering
alert emails for testing, and checking/modifying users and access
rights.

New CLI to support it, `services/simulate_equipment_event.py` (`add`/
`remove` subcommands) — formalizes the ad hoc scratchpad pattern used
repeatedly this session for live-testing into a proper, reusable tool.
Goes through the exact same `StateManager.process_observation()` path
the live poller uses, with an optional `--notify` to also exercise the
real `AlertEngine`/email send. Equipment IDs are required to start with
`TEST-` (enforced, not just documented) so test data can never be
mistaken for a real machine and is always safe/obvious to clean up.

9 new tests (id-prefix guard, healthy/malfunction/custom-fault-type,
no-duplicate-incident-on-repeat, real `--notify` send via mocked SMTP,
remove + cascade, remove-nonexistent-is-a-no-op). 264/264 passing
overall. Live-verified against the real demo DB: added a TEST- machine
healthy, transitioned it to malfunction with `--notify` (confirmed a
real, non-console-fallback email sent), then removed it.

**Follow-up (same day): fixed the recurring "database is locked"
crash for real.** User asked why the dashboard showed stale data;
found `monitoring.combined_worker` had silently crashed 3.5 hours
earlier — same `sqlite3.OperationalError: database is locked` bug
flagged (but not yet fixed) after its first occurrence — and nothing
had restarted it since. Root cause confirmed directly, not assumed:
SQLite's default rollback-journal mode takes an exclusive lock for the
whole duration of a write, so the worker (writer) and dashboard
(reader) hitting the same `data/demo.db` file concurrently intermittently
fails outright instead of waiting; `asyncio.gather()` in
`monitoring/combined_worker.py` then propagates that one exception and
kills both loops together, even though only the orders side failed.

Fix, in `db/base.py`'s `init_engine()`: enables SQLite **WAL (Write-
Ahead Logging) mode** on every connection (lets readers and a single
writer coexist without blocking each other — the actual fix) plus a
**30s `busy_timeout`** as a safety net for the rarer writer-vs-writer
case. No effect on Postgres (guarded by `settings.database_url.startswith("sqlite")`,
same pattern already used for `check_same_thread`).

4 new tests, including a direct regression reproduction: two live
sessions against the same SQLite file, one holding a write open, the
other reading concurrently — passes under WAL, would have raised
`database is locked` under the old default. 268/268 passing overall.
Live-verified against the real `data/demo.db`, not just tests: restarted
both processes, confirmed `PRAGMA journal_mode` reports `wal` and
`PRAGMA busy_timeout` reports `30000` on the actual file, then hit
`/healthz` repeatedly while the worker was mid-poll-cycle with no
errors.

## [3.0.0] — 2026-08-26

**Version 3.0: the email alerting system is fully active end-to-end** —
real SMTP delivery (not just the console-fallback channel), a flat
admin-managed recipient list alongside the existing admin/subscription
model, and a second tabular "Critical Faults Digest" notification. Also
in this release: Cost Management, Staff & Leave Management, cloud
deployment infrastructure, and an IST-localized dashboard. Everything
below was live-verified against the real target site and/or a real
inbox, not just unit tests. 255/255 automated tests passing.

Two more admin-only tools, both explicitly restricted to the `admin`
role (`operations`/`venue_partner` get `403`, same as `/users`):

- **Cost Management** (`/costs`, `costs/` package) — log operating costs
  against standard categories (Oranges, Glass, Straws, Sealing Films,
  Staff Salaries, Rent, Cleaning Items) or a custom one via "Others";
  vendor name is skipped for Staff Salaries and defaults to a filterable
  `"UNSPECIFIED"` placeholder (not a bare NULL) if left blank elsewhere.
  Summarized over Daily/Weekly/Monthly/YTD or a **user-provided custom
  date range**, with independent breakdown by category/vendor/item name.
- **Staff & Leave Management** (`/staff`, `staff/` package) — a staff
  roster with employment start/end dates, leaves logged by the admin on
  a staff member's behalf, and a monthly summary that explicitly
  highlights any staff with more than 2 leave days that month (correctly
  clipping leaves that span a month boundary).
- `backend/period_utils.py` extracted from Order Summary's period-range
  logic (now shared by both features) and gained a `custom` period —
  Order Summary can use it too, though its UI doesn't expose it yet.
- Cost Management gained, per follow-up request: filter dropdowns to
  view raw data for a selected category/vendor/item (narrows both the
  rollup and a new Raw Entries table beneath it); edit and delete on any
  logged entry, with the same category/vendor resolution rules as
  creation reused for edits (`_resolve_category_and_vendor`). A
  never-edited entry's `updated_at` stays `NULL`.

Also resolved the RBAC open question from 2.0.0: **operations staff do
not see Order Summary at all**, confirmed — no code change needed, that
was already how `require_venue_partner` was built.

**Follow-up (same day):**
- Staff gained `department`/`sub_department` (free text, e.g. "Operations"
  / "Logistics") — shown in the roster, set on creation, suggested via a
  `<datalist>` of previously-used values so spelling stays consistent
  without a hard-coded list.
- Cost Management's Raw Entries table is now **hidden by default** —
  a "Raw data: Show / Hide" radio pair controls it explicitly, per
  follow-up request ("should only be displayed if explicitly asked").
  The category/vendor/item filters and rollup breakdown are unaffected
  either way.

See README's "Cost Management and Staff & Leave Management" section for
the full picture. 196/196 tests passing.

**Follow-up (2026-08-24): `orders/realtime_worker.py`.** Answers "why
does today's order data not show up?" — `orders/backfill.py` only ever
processes a fully-elapsed date on purpose (a day still accumulating
orders would get permanently cached on a partial snapshot otherwise), so
today never appears until tomorrow's backfill run. This new script is
the deliberate exception: always targets "today" in the target's own
UTC+8 calendar (recomputed every cycle) and unconditionally overwrites,
whether or not that date already has a `SUCCESS` row. `python -m
orders.realtime_worker --loop` (default every 5 minutes) runs as a
fourth long-lived process alongside the equipment worker and dashboard.
Handles the UTC+8 day-rollover case explicitly — the day that just ended
gets one final refresh before the new day starts being tracked, so
orders placed in the last few minutes before midnight aren't lost.
9 new tests; 205/205 passing overall. Live-verified: today's data was
confirmed missing, then appeared (215 raw → 172 qualifying orders, 3
groups) within seconds of running `--once`; `--loop` left running.

**Follow-up (same day): reporting timezone switched from the target's
UTC+8 to IST.** Per explicit request — "I want to see glasses sold today
(as per Indian timezone)". `order_date` (the single source of truth for
every day boundary in the whole `orders/` pipeline) is now computed in
IST (`orders/mapping.py`'s `IST_TZ`), not the target's own UTC+8
(`TARGET_TZ`, which is unchanged and still needed internally). Since IST
is 2.5 hours behind UTC+8, one IST day always straddles two of the
target's own UTC+8 days — `orders/client.py`'s `fetch_day()` now queries
both and filters to the IST match, rather than trusting the target's own
bucketing directly. `orders/realtime_worker.py`'s `today_utc8()` became
`today_ist()`.

**Full history re-backfilled** under IST boundaries the same day
(`--force`, 2026-05-02 through 2026-08-23) so there's no seam between
old UTC+8-bucketed and new IST-bucketed data — every stored day uses the
same convention. Caught and fixed a bug of my own along the way: the
"unexpected order_date" sanity-check warning in `fetch_day()` had the
wrong expected-date window (only looked forward a day, not also
backward) and fired on every single legitimate cross-day spillover —
cosmetic only, the actual returned/stored data was correct throughout,
but fixed and covered by a regression test
(`test_fetch_day_legitimate_spillover_does_not_warn`) before trusting
the logs again. Also cleaned up two stray `OrderSummary`/
`OrderSummaryRun` rows for "today" and a not-yet-existing-in-IST future
date, left over from the old UTC+8-based realtime worker's last cycle
before being replaced.

18 new/updated tests (IST day-boundary computation with an injectable
`now`, `orders/client.py`'s two-target-day fetch-and-filter logic via
`httpx.MockTransport`, the spillover-warning regression). 213/213
passing overall. Live-verified: today's IST total is 152 orders (3
machines) after the fix and re-backfill.

**Follow-up (same day): edit staff roster entries.** Per explicit
request — "Suppose I mistakenly marked an employee as left; now I want
to reset the employee status to previous state using edit functionality.
Also, show mark as left option once edited and employment remains
active." Every roster row now has an Edit link (`/staff/{id}/edit`,
`backend/templates/staff_edit.html`) that can change any field —
clearing the employment end date is exactly how a mistaken "Mark as
left" gets undone, and the roster's existing `not
employment_end_date` check is what naturally brings "Mark as left" back
into view once they're active again (no new conditional needed). 7 new
tests. 220/220 passing overall. Live-verified against the demo DB's own
Priya Sharma, who happened to already be marked left (2026-08-24) —
edited her end date blank, confirmed she shows active with "Mark as
left" restored on the roster.

**Follow-up (same day): "build the email alerting system."** Turned out
this already existed and was already running — `services/alert_engine.py`
+ `services/notification_service.py`, wired into `monitoring/worker.py`
since Phase 4, detecting a MALFUNCTION/OFFLINE transition exactly once
and composing a message with the machine's name/ID/code/health/fault
text/severity/detected time. Verified this with real evidence rather
than taking it on faith: found 8 real `FaultIncident` rows already in
the demo DB, 3 with `notification_sent=True` from real incidents.

What was actually missing, clarified by asking rather than guessing: a
**flat, admin-managed recipient list** not tied to a dashboard account
(the existing model requires either the `admin` role or a per-user
`/subscriptions` row). Added `AlertRecipient` (`db/models.py`) and
`/alert-recipients` (admin-only CRUD — add/deactivate/reactivate an
email, optional reference name). `AlertEngine.get_recipients()` now
unions in every active `AlertRecipient` whenever severity is Critical —
same trigger as the admin-always rule, additive to it, not a
replacement.

Found and fixed a **pre-existing UI bug** while building the recipients
page: `form.stacked-form`'s CSS centered every such form, not just the
login page it was meant for — every "Add X" form on Cost Management,
Staff & Leave, and now Alert Recipients rendered indented/centered
instead of left-aligned. Fixed at the CSS root (`.login-box` already
handles login's own centering independently) instead of patching yet
another template with an inline override.

18 new tests (recipient resolution incl. Critical-only/inactive/dedup-
with-admins, `/alert-recipients` CRUD + RBAC). 233/233 passing overall.
Live-verified end-to-end against the real demo DB: simulated a fresh
MALFUNCTION through the exact code path `monitoring/worker.py` uses,
watched the composed message list all three real recipients (the admin
+ two `AlertRecipient` emails, one of which — `support@refresha.in` —
was added by the user directly while this was being built), then
cleaned up the synthetic equipment/incident afterward. **The one
remaining real gap, unchanged from Phase 4**: no SMTP credentials are
configured, so every alert still only reaches the console log, not a
real inbox.

**Follow-up (2026-08-25): cloud deployment.** Resolves the long-🔖
bookmarked "host on an always-on cloud environment" gap — the user's
first-ever cloud deployment, so this used plan mode (a Plan agent for
research + design, live verification of every claim before trusting it,
one clarifying question to the user on a real architectural tradeoff)
rather than just executing.

Recommended **Railway** over Render/Fly.io — cheapest and simplest for
this workload's shape (one web process + one background worker sharing
a filesystem, managed Postgres, no VPC/IAM knowledge needed); reasoning
in `docs/DEPLOYMENT.md`.

**Architecture is 2 deployed services, not 3**, even though there are 3
local `--loop` scripts. Real finding, not assumed: verified
`orders/client.py`'s `OrdersClient` only ever loaded its session cookies
once, at construction — unlike `monitoring/lightweight_client.py`'s
`LightweightTargetClient`, which reloads after every re-auth — and that
cloud platforms attach a persistent volume to exactly one service each.
Running the two workers as separate deployed services would have left
`orders-worker` permanently stuck on its very first session, quietly
breaking Order Summary the next time a re-login happened. Asked the
user rather than deciding alone: fix it properly, or document a manual
workaround? Chose the fix:
- `OrdersClient.reload_cookies()` (new) — mirrors
  `LightweightTargetClient`'s existing pattern.
- `orders/realtime_worker.py`'s `run_cycle()` now calls it once per
  cycle — cheap, correct whether or not a re-auth just happened.
- `monitoring/combined_worker.py` (new) — runs `monitoring.worker`'s
  and `orders.realtime_worker`'s existing loops concurrently via
  `asyncio.gather()`, zero logic duplicated, both loops still work
  completely unchanged standalone for local dev. This is now also the
  normal way to run both locally (replaced the two separate background
  processes in this session's own dev setup).

New `Dockerfile` (base `mcr.microsoft.com/playwright/python:v1.47.0-jammy`,
version-matched to the `playwright==1.47.0` pin — ships headless
Chromium pre-installed, avoiding ~20 manual apt packages), `.dockerignore`,
and a public `GET /healthz` (`backend/api/health.py`) — the existing
`/api/monitoring/status` requires login and isn't suitable for a
platform health check. Full step-by-step walkthrough (account creation
through verification, written for a first-timer): `docs/DEPLOYMENT.md`.

5 new tests (`reload_cookies()` correctness, `run_cycle()` calling it
exactly once per cycle, `/healthz`). 238/238 passing overall.
Live-verified **against the real target, not just tests**: ran
`monitoring.combined_worker --loop` locally, confirmed both loops' log
lines genuinely interleaved in one stream (equipment poll cycles +
order refreshes, not just started-and-forgotten), confirmed `/healthz`
responds with no login cookie required. **Honestly flagged, not
glossed over**: the Dockerfile itself was never build-tested — no
Docker available in this dev environment — so the image build is
unverified until the first real Railway deploy, even though the Python
code path it runs was verified directly. Deployment execution itself
(account creation, clicking through Railway's own UI, billing) is the
user's own next step, not something this session could do on their
behalf.

**Follow-up (2026-08-26): remove demo equipment, display IST on the
dashboard.** The 2 synthetic `[DEMO]`-labeled equipment rows (added
earlier for screenshot/badge coverage, never a seed script — just a
one-off DB write) were deleted from `data/demo.db`; Fleet Overview now
shows only the 6 real machines. Every timestamp on the equipment
monitoring pages was being displayed raw, i.e. in UTC (what
`db/models.py`'s `_utcnow()` stores) — per explicit request, these now
render in IST via a new `ist` Jinja filter (`backend/templating.py`)
that reuses `orders/mapping.py`'s existing `IST_TZ` rather than a second
timezone constant, applied across `dashboard.html`, `equipment_detail.html`,
`faults.html`, and `fault_detail.html`. Display-only — stored values and
the JSON `/api/monitoring/status` API are unchanged (still UTC).
4 new tests. 242/242 passing overall. Live-verified: restarted both
processes, confirmed the dashboard shows exactly 6 machines with every
timestamp reading e.g. "26 Aug 2026, 12:23:54 IST".

**Follow-up (2026-08-26): real SMTP verified, then Critical Faults
Digest.** The user added real SMTP credentials to `.env`; a live send
test to `support@refresha.in` succeeded — confirmed via the
`services.notification_service: Email sent to ...` log line, which only
logs after `smtplib` actually completes connect/login/send (not the
`[console-channel]` fallback text). This closes the one real gap that's
existed since Phase 4; the alerting system itself required no changes.

Then, per explicit request — "send email notifying the equipment with
critical faults... always... tabular format specifying the fault
details" — added a second, distinct notification: `services/fault_digest.py`,
one email listing **every** currently-Critical machine as a table
(Machine, Equipment ID, Equipment Code, Fault, Health, Since (IST),
Duration), not the existing per-incident instant alert (kept unchanged,
one machine per email, fired once on detection). Refactored
`NotificationService`/`EmailNotificationChannel` to support an optional
`html_body` (proper `multipart/alternative` — plain-text table as
fallback, real `<table>` for HTML-rendering clients) rather than
building yet another ad hoc email path. Recipients reuse
`AlertEngine.get_recipients(session, equipment_id=None,
severity="Critical")` — the same audience every other Critical
notification already uses. New CLI: `python -m services.send_fault_digest`.
Refactored the IST formatting helper out of `backend/templating.py` and
into `orders/mapping.py` (`format_ist()`) so this module and the
dashboard's `ist` Jinja filter share one implementation instead of two.

19 new/updated tests (digest content incl. HTML-escaping and duration
formatting, recipient resolution, multipart-vs-plain-text email
construction). 255/255 passing overall. Live-verified against the 3
real active OFFLINE incidents (Warehouse, REFRESHA 2, REFRESH-1): sent
successfully, HTML table rendering confirmed via screenshot.

**Follow-up (same day): live end-to-end HEALTHY→MALFUNCTION scenario
test.** Per explicit request — added a demo machine as HEALTHY through
the exact `StateManager.process_observation()` path the live poller
uses, waited ~1 minute, then transitioned it to MALFUNCTION. Confirmed
`incident_opened=True` and a real (non-console-fallback) email sent,
screenshotting both states. Cleaned up the demo equipment afterward.

Two real, unrelated issues surfaced and resolved along the way:
- A one-off script run without the `DATABASE_URL="sqlite:///data/demo.db"`
  prefix every other process in this project uses fell through to the
  unused Postgres config path in `.env` and failed to authenticate —
  initially misdiagnosed as a corrupted credential; corrected once the
  actual cause (a missing env var on that one invocation, not a broken
  password) was found. No credential was actually broken or reset.
- `monitoring.combined_worker` crashed mid-session with `sqlite3.
  OperationalError: database is locked` writing to `order_summary` —
  concurrent SQLite access between the worker and the dashboard process.
  Restarted successfully; the underlying concurrency risk (SQLite under
  concurrent writers, especially on Windows) is real and noted as a
  Phase 6 candidate (WAL mode + busy_timeout, or a real Postgres
  instance) rather than silently left for it to recur.

## [2.0.0] — 2026-08-24

Everything built on top of the original equipment-monitoring app (1.0.0):
an independent order-analytics feature, full role-based access control,
and a rebrand to the company's own visual identity. All 142 automated
tests passing; every piece below has also been verified against the real
target site or a live local server, not just in tests.

### Added

- **Order Summary feature** (`orders/` package, two new tables). Daily
  order data from Order Management → Order Information, filtered to
  `order_status=Completed AND delivery_status=Success`, stored at the
  finest useful grain — one row per `(date, device_app, price,
  pay_type)`, so a mid-day price change produces separate rows instead of
  a blended average. Every other view (aggregate, machine-wise,
  price-wise, pay-type-wise, or any combination) is a rollup computed at
  query time (`orders/rollup.py`), not a separately stored table.
  - Dashboard tab at `/orders/summary`: Daily/Weekly/Monthly/YTD period
    selector, independent breakdown checkboxes (machine/price/pay type,
    any combination), an always-on aggregated trend chart plus an
    optional by-machine trend chart (Chart.js via CDN).
  - Full historical backfill: 2026-05-02 through 2026-08-23 (114 days,
    zero failures) — `python -m orders.backfill --start … --end …`.
  - A real, previously-undocumented target-app detail surfaced building
    this: the `createtime` date-range filter uses **UTC+8 day
    boundaries**, not UTC/IST/local time (see
    `docs/target_application_integration_spec.md`).
  - **Admin-only Revenue and Oranges/Glass** metrics — `orders/rollup.py`
    computes `revenue = orders × avg price` and `oranges_per_glass =
    total oranges / orders` as derived properties, rendered only for the
    `admin` role.
- **Role-based access control** (`db/models.py`'s `UserRole`, enforced via
  `backend/deps.py`). Three roles: `admin` (unrestricted), `operations`
  (equipment monitoring only — Dashboard/Active Faults/My Alerts, no
  order data), `venue_partner` (Order Summary only, scoped to their own
  venue). Enforced at the route level, not just hidden nav links; nav and
  post-login redirect are role-aware too.
- **Venue mapping** — new `venue_mapping` table (`machine_name` →
  `venue_provider`, matched case-insensitively), seeded via `python -m
  db.seed_venue_mapping`:
  `PNR → PNR Felicity`, `NEXUS → Forum Kormangala`, `Gravity → Prestige Tech Park`.
  A venue partner with no venue assigned (or one mapped to zero machines)
  gets an explicit message rather than a silently-empty page.
- **Rebrand to JOY (joyjuice.in)** — warm orange/cream palette (`#fea419`
  brand, extracted from the live site), Poppins typeface, rounded
  cards/pill buttons, plus a working **dark/light theme toggle**
  (`backend/static/theme.js`, no-flash pre-paint script, persisted via
  `localStorage`, reachable from every page including login). Same
  FastAPI + Jinja2 backend throughout — restyle only, no JS framework or
  build step added (explicit choice over a full React rewrite).
- **`docs/prompt_logs.md`** — a chronological, mostly-verbatim record of
  every user prompt that shaped this build, maintained going forward
  rather than left to erode across context compactions.

### Fixed

- `LightweightTargetClient.is_session_valid()` was probing the dashboard
  shell page, which returns HTTP 200 even with zero cookies (a
  false-positive trap) — switched to probing the actual list API.
- Orders with a null `device.name` in the raw feed were bucketed as
  `"UNKNOWN"`; confirmed with the account owner that these are all Nexus
  machine data, so the fallback now maps to `"NEXUS"` (48 already-cached
  days were re-fetched and recomputed).

### Changed

- CAPTCHA login can now be fully automated (`AUTO_SOLVE_CAPTCHA=true`) —
  an explicit, disclosed, account-owner-authorized deviation from the
  original 1.0.0 spec, specific to a confirmed bug in this one target
  (any 4 characters pass CAPTCHA validation). Defaults to `false` and the
  human-in-the-loop path is unchanged for any other target.
- Steady-state polling moved off a persistent Playwright browser onto a
  plain `httpx` client (`monitoring/lightweight_client.py`); a real
  browser now launches only transiently, for the CAPTCHA login step
  itself.

## [1.0.0] — 2026-08-23

Initial build, from the original specification: session-aware monitoring
of a third-party equipment-management web app, a canonical health-state
engine, incident detection/escalation/resolution, email alerting, and a
server-rendered FastAPI + Jinja2 dashboard.

- Phase 1 — Target application discovery (confirmed live).
- Phase 2 — Monitoring proof of concept: session reuse, real equipment
  extraction via the target's own JSON API.
- Phase 3 — Database + state engine (`services/health_engine.py`,
  `services/state_manager.py`), live-verified against SQLite.
- Phase 4 — Alerting (`services/alert_engine.py` +
  `services/notification_service.py`), confirmed firing on real
  incidents; no real email actually sent (no SMTP credentials available).
- Phase 5 — Web dashboard, live-verified with real HTTP traffic.
- Phase 6 (production hardening) was explicitly out of scope for 1.0.0 —
  see Pending below.

---

## Pending / known gaps (as of 2.0.0)

Not silently dropped — these are the open items, roughly in the order
they'd matter for taking this beyond local/demo use. See
`docs/NEXT_SESSION_PROMPT.md` for more detail on each.

**Resolved:**
- Whether `operations` staff should see Order Summary — **no, confirmed
  2026-08-24.** Matches how it was already built: `require_venue_partner`
  only admits `admin`/`venue_partner`, `operations` gets `403`. No code
  change needed, just closing out the open question.

**Needs a decision from the user:**
- The `Warehouse` device (`avg_price = ₹0.01`, 10 orders total) is still
  unexplained — real data, not a bug, but not investigated.
- Chart.js trend-line colors are still generic (blue/green/orange/red),
  not derived from the JOY brand palette — flagged, not changed, since it
  wasn't asked for.

**Verification gaps (blocked on infrastructure/credentials, not code):**
- Never run against real PostgreSQL — only SQLite (dev/test/demo). No
  Docker/Postgres available in this environment so far.
- Never sent a real email — `NotificationService`'s SMTP path is
  unit-tested with `smtplib` mocked only; no SMTP credentials available.
- Order Summary totals have not been independently cross-checked against
  the target app's own UI (only against this app's own CLI/DB/dashboard
  agreeing with each other).

**Product/process gaps introduced by this session's features:**
- No in-app way to edit an existing user's role or venue — `/users` only
  sets these at creation time; changing them today means a direct DB
  edit.
- No self-service invite/password-set flow for a new venue partner — an
  admin sets their initial password directly.
- The demo accounts created for RBAC verification (`demo@example.com`,
  `ops_demo@example.com`, `venue_demo@example.com`) use weak, throwaway
  passwords — fine for local demo, should be rotated or removed before
  this ever points at anything beyond this machine.

**Phase 6 — production hardening (not started, pick based on actual
need, not all of this unprompted):**
- Alembic migrations (schema is currently hand-patched — see README's
  RBAC section for the manual `ALTER TABLE` this release already needed).
- Retry-with-backoff within a single poll cycle.
- Browser crash/restart recovery for the re-authentication path.
- Structured logging, a health-check endpoint, Prometheus/Grafana.
- Docker images for the worker + backend, Nginx reverse proxy.
- A secrets manager in place of plain `.env`.
- Documented DB backup/restore procedure.
- Rate limiting on login/API routes; a CSRF token on mutating forms
  (currently relies only on `SameSite=Lax`).
- 🔖 Always-on cloud hosting, so the app doesn't fully stop whenever the
  host machine sleeps (raised earlier, not yet acted on).

**Lower priority, needs user direction before building:**
- Multiple monitoring profiles (more than one target account).
- Wire the target API's rich per-component `this_fault` detail into
  incidents (currently only a flat `fault_type` string is kept).
- Confirm real-world fault/shortage vocabulary once this account
  produces an actual non-"Normal" reading outside the spec's examples.
- Configurable dashboard session lifetime (fixed at 7 days).
