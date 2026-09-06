# Deploying to a VPS (Hostinger KVM, self-managed)

Written 2026-08-28. This is a sibling to [`DEPLOYMENT.md`](DEPLOYMENT.md)
(Railway) — both remain valid paths. This one trades Railway's managed
simplicity for full root-level control and a flat recurring cost, at
the price of the user now owning OS patching, TLS renewal, and firewall
correctness themselves.

## Why a VPS here (vs. Railway)

Shared/"cPanel" hosting was ruled out early: this app needs to launch
headless Chromium via Playwright, and a normal Linux process needs
enough OS-level access (installing system packages, running arbitrary
binaries) that virtually all shared hosting plans forbid — confirmed
directly from Hostinger's own docs, Python itself requires root access
their shared plans don't grant. A **Hostinger KVM VPS** is a real
virtual machine with root SSH access — same category as a
DigitalOcean/Linode droplet — so everything Railway's containers do
implicitly (build the Dockerfile, run arbitrary commands), this path
does explicitly, by hand, once.

## Architecture recap

Same two application processes as the Railway deployment, same reason
they're combined into one (see `DEPLOYMENT.md`'s explanation of
`monitoring/combined_worker.py` / `OrdersClient.reload_cookies()` —
unchanged, not repeated here), now expressed as three Docker Compose
services on one VPS instead of two Railway services + one managed
plugin:

- **`web`** — `uvicorn backend.main:app`, the Dockerfile's default
  `CMD`. Published only to `127.0.0.1:8000` on the VPS — Nginx is the
  only thing that ever talks to it directly; the public internet
  reaches it through Nginx, never the port itself.
- **`worker`** — same image as `web`, command overridden to
  `python -m monitoring.combined_worker --loop`. No published port at
  all. Mounts a named volume at `/app/data` (the Playwright session
  file lives there) — `web` does **not** mount this volume; a
  repo-wide check confirmed nothing under `backend/` ever reads or
  writes `STORAGE_STATE_PATH`, only `monitoring/`/`orders/` code does,
  and both only run inside `worker`.
- **`postgres`** — same image/config as the existing dev
  `docker-compose.yml`, with one change: published to `127.0.0.1:5432`
  instead of all interfaces (see below).

Unlike Railway, there's no managed Postgres plugin — this VPS runs its
own Postgres in a third container, on the same Docker network as
`web`/`worker`, reachable by them at the Compose service name
`postgres:5432` (Docker Compose's built-in DNS), never over a
host-published port.

### Why bind `127.0.0.1:5432:5432` and `127.0.0.1:8000:8000` (not `0.0.0.0`)

The existing dev `docker-compose.yml` publishes Postgres as
`"${DB_PORT:-5432}:5432"` — no host IP given, which Docker publishes on
*all* interfaces. That's harmless on a laptop with no public IP sitting
behind home-router NAT. It is genuinely dangerous on a VPS: the VPS has
a real public IP, so an unauthenticated Postgres port would be directly
reachable and scannable from the open internet the instant the
container starts — *before* `ufw` rules even matter, because Docker
manipulates `iptables` directly in a way that's a well-known gap in
`ufw`-only firewalling. Binding to `127.0.0.1` explicitly is the actual
fix (the port simply doesn't exist outside the VPS itself), with `ufw`
as a second, redundant layer. This is the one concrete security
correction the production compose file makes versus the dev file —
flagged here explicitly, not left implicit.

## Step-by-step

### Step 0 — Provision the VPS

If you've already reached Hostinger's OS-selection screen and picked
**Ubuntu 24.04 LTS**, that part's done. After provisioning finishes,
Hostinger shows (or emails) the VPS's public IP and a root password.
Note the IP now — it's needed for the DNS step and every SSH command
below. Replace `<VPS_IP>` throughout with that real value.

### Step 1 — First SSH login and basic hardening

```bash
ssh root@<VPS_IP>
```

Accept the host key prompt (first connection only). Then, as root:

```bash
# Create a non-root sudo user — never operate as root day-to-day
adduser deploy
usermod -aG sudo deploy

# Copy your SSH key so `deploy` can log in without a password
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

*If you don't have an SSH keypair yet on your own machine*, generate
one locally first (`ssh-keygen -t ed25519`) and use
`ssh-copy-id root@<VPS_IP>` before the `rsync` step above, so the key
material actually exists to copy.

Now edit SSH config to disable root login and password auth:

```bash
sudo nano /etc/ssh/sshd_config
```

Set (or confirm) these two lines:
```
PermitRootLogin no
PasswordAuthentication no
```

```bash
sudo systemctl restart ssh
```

**Before closing this session**, open a *second* terminal and confirm
`ssh deploy@<VPS_IP>` works and can `sudo` — if it can't, fix it from
the still-open root session before disconnecting. Getting this wrong
and closing the root session first means losing access entirely.

From here on, every command is run as `deploy` over
`ssh deploy@<VPS_IP>`, not root.

### Step 2 — Firewall (`ufw`)

Ubuntu ships `ufw` preinstalled. Allow only what's needed:

```bash
sudo ufw allow OpenSSH        # port 22
sudo ufw allow 80/tcp          # HTTP (needed for Certbot's initial challenge)
sudo ufw allow 443/tcp         # HTTPS
sudo ufw enable
sudo ufw status verbose
```

Confirm the output lists exactly 22, 80, 443 as `ALLOW` — **do not**
add rules for 5432 or 8000; those stay unreachable from the internet by
design (see the `127.0.0.1` binding above — `ufw` is the second layer,
not the only one).

### Step 3 — Install Docker Engine + Compose plugin

Hostinger's Ubuntu image doesn't ship Docker. Use Docker's official
install script (simplest, matches Docker's own current recommendation
for Ubuntu 24.04):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
```

Log out and back in (`exit`, then `ssh deploy@<VPS_IP>` again) for the
group membership to take effect, then confirm:

```bash
docker --version
docker compose version
```

### Step 4 — DNS: point a subdomain at the VPS

Earlier exploration in this project looked at a Cloudflare Tunnel path
for exposing this app without a public IP. **A VPS makes that entire
approach unnecessary** — it has a real static public IP, so this is
just a normal DNS A record, nothing more:

1. Log in to Hostinger's hPanel, go to the DNS zone editor for
   `joyjuice.in` (already hosted on Hostinger's own nameservers —
   confirmed via `nslookup joyjuice.in`, which returned
   `ns1/ns2.dns-parking.com`).
2. Add an **A record**: name `equipment`, value `<VPS_IP>`, TTL default
   (or 300s if you want faster propagation while testing).
3. Wait for propagation (`nslookup equipment.joyjuice.in` — a few
   minutes typically, can be up to an hour).

This does **not** touch `refresha.in` or `joyjuice.in`'s existing
website or email records — it only adds one new, unrelated subdomain
record. Cloudflare isn't required anywhere in this path; it could
optionally still be layered in front later purely as a CDN/WAF, but
nothing here depends on it.

### Step 5 — Clone the repo and set up `.env`

```bash
sudo mkdir -p /opt/equipment-monitor
sudo chown deploy:deploy /opt/equipment-monitor
git clone https://github.com/<your-username>/<repo-name>.git /opt/equipment-monitor
cd /opt/equipment-monitor
cp .env.example .env
nano .env
```

Fill in every value from the table below. This `.env` file lives only
on the VPS's disk (`/opt/equipment-monitor/.env`) — it is never
committed (matches the repo's existing `.gitignore` pattern, which
already excludes `.env`/`.env.*` except `.env.example`).

| Variable | Production value | Differs from local `.env`? |
|---|---|---|
| `TARGET_BASE_URL`, `TARGET_LOGIN_URL`, `TARGET_USERNAME`, `TARGET_PASSWORD` | same as local `.env` | no |
| `STORAGE_STATE_PATH` | `data/storage_state/session.json` | no — resolves to `/app/data/storage_state/session.json` inside the `worker` container |
| `HEADLESS` | `true` | **yes** — no display exists on a VPS |
| `AUTO_SOLVE_CAPTCHA` | `true` | **yes** — the same pre-existing, explicitly authorized (2026-08-23) exception used for the Railway deployment; no human is present to solve a CAPTCHA on a headless server |
| `AUTH_MANUAL_TIMEOUT_SECONDS`, `POLL_INTERVAL_SECONDS`, `POC_ITERATIONS` | same as local `.env` | no |
| `DB_HOST` | `postgres` | **yes** — the Compose service name; `web`/`worker` reach Postgres over the Docker network by service name, not `127.0.0.1` (that bind is only for reaching it *from the VPS host itself*, e.g. for `psql` debugging) |
| `DB_PORT` | `5432` | no |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | choose real values, not the `equipment_monitor`/`equipment_monitor` dev defaults | **yes** — dev defaults are fine for a laptop-only Postgres nothing else can reach; on a VPS, even loopback-only, use a real generated password |
| `DATABASE_URL` | leave **unset** | — unlike Railway (which sets this to `${{Postgres.DATABASE_URL}}`), here the discrete `DB_*` fields are used directly; `monitoring/config.py`'s `Settings.database_url` property only overrides with `DATABASE_URL` if it's non-empty, so leaving it blank correctly falls through to `DB_*` |
| `RESEND_API_KEY`, `RESEND_FROM_EMAIL` | from your Resend dashboard (resend.com/api-keys); `RESEND_FROM_EMAIL` must be on a domain verified there | recommended — confirmed live (2026-09-06) that Railway blocks outbound SMTP entirely; a self-managed VPS you control may not have that restriction (worth testing with the same `timeout 5 bash -c '</dev/tcp/smtp.gmail.com/587'` check from `docs/DEPLOYMENT.md`'s troubleshooting before assuming either way), but Resend works regardless and keeps both deployment paths on the same code path |
| `SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_EMAIL`/`SMTP_USE_TLS`, `ALERT_ON_ESCALATION`, `SEND_RECOVERY_NOTIFICATIONS` | same as local (Resend takes priority when both are set) | no |
| `WEB_SECRET_KEY` | fresh output of `python3 -c "import secrets; print(secrets.token_hex(32))"` (run on the VPS itself, or any machine with Python 3) | **yes** — must never match the local dev value, or the Railway deployment's value if both ever run at once |

### Step 6 — `docker-compose.prod.yml`

Already committed at the repo root (`docker-compose.prod.yml`) — the
three-service topology described above. The existing
`docker-compose.yml` stays untouched (still the local-dev,
Postgres-only file); every command below is explicit about
`-f docker-compose.prod.yml` so the two are never confused.

### Step 7 — First build and start

```bash
cd /opt/equipment-monitor
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Expect three containers, all `Up` (postgres additionally `healthy`).
*If `web` or `worker` restart-loops:* check
`docker compose -f docker-compose.prod.yml logs web` (or `worker`) —
almost always a blank/wrong required env var, same failure mode as the
Railway deployment.

### Step 8 — Create the schema and seed the real admin

```bash
docker compose -f docker-compose.prod.yml exec worker python -m db.init_db
docker compose -f docker-compose.prod.yml exec worker python -m db.seed_admin \
  --name "Ashish" --email 1206ashish656@gmail.com \
  --password "<choose a genuinely strong password now>"
```

`exec` runs inside the already-running `worker` container (either
`web` or `worker` would work — both share the same image and reach the
same Postgres; `worker` is used here purely for consistency). **Do
not** seed the local demo's weak-password accounts
(`demo@example.com`/`admin123`) into production.

### Step 9 — Install Nginx and get a TLS certificate

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/equipment`:

```nginx
server {
    listen 80;
    server_name equipment.joyjuice.in;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable it and reload:

```bash
sudo ln -s /etc/nginx/sites-available/equipment /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

*Confirm Step 4's DNS record has propagated*
(`nslookup equipment.joyjuice.in` should return `<VPS_IP>`) before the
next command — Certbot's HTTP-01 challenge needs the domain to actually
resolve here first.

```bash
sudo certbot --nginx -d equipment.joyjuice.in
```

Certbot rewrites the Nginx config in place to add the 443 `server`
block, the cert paths, and an HTTP→HTTPS redirect — you don't hand-edit
that part. It also installs a systemd timer (`certbot.timer`, already
enabled by the package) that renews automatically before the 90-day
expiry — no cron job to set up yourself. Confirm the timer exists:

```bash
systemctl list-timers | grep certbot
```

### Step 10 — Verify

- `curl -I https://equipment.joyjuice.in/healthz` → `HTTP/2 200`,
  `{"status": "ok"}` — confirms Nginx → `web` → the FastAPI
  `lifespan()` startup (which already confirmed Postgres connectivity,
  since `create_all()` would have crashed the container otherwise).
- Visit `https://equipment.joyjuice.in/login` in a browser, sign in
  with the admin account from Step 8.
- Confirm the dashboard shows real equipment data (proves `worker` →
  Postgres → `web` are all correctly connected).
- Confirm the Order Summary tab shows today's data (proves
  `OrdersClient.reload_cookies()` is working with no manual
  intervention, same as the Railway checklist).
- `docker compose -f docker-compose.prod.yml logs -f worker` — confirm
  both the equipment-poll loop's and the order-refresh loop's log lines
  appear interleaved in one stream (roughly every 60s and every 300s
  respectively) — proof both loops are genuinely running concurrently
  in one process, exactly as in the Railway deployment.
- Confirm Postgres is **not** reachable from outside the VPS: from your
  own machine (not the VPS), run `nc -zv <VPS_IP> 5432` or, in
  PowerShell, `Test-NetConnection <VPS_IP> -Port 5432` — expect a
  connection **refusal/timeout**, not a connection. If it connects, the
  `127.0.0.1:5432:5432` bind in `docker-compose.prod.yml` wasn't
  applied — re-check with `docker compose -f docker-compose.prod.yml
  config`.

## Ongoing redeploy workflow

Deliberately manual, no CI/CD — matches this project's established
"scope narrow, defer automation" pattern (see `DEPLOYMENT.md`'s own
"Explicitly deferred" list). To ship a code change:

```bash
ssh deploy@<VPS_IP>
cd /opt/equipment-monitor
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

This rebuilds only what changed (Docker layer caching) and restarts
`web`/`worker` with a brief restart (not a blue/green swap — acceptable
at this scale). Postgres is untouched unless its own image tag changes.
If a schema change is ever needed, re-run
`docker compose -f docker-compose.prod.yml exec worker python -m db.init_db`
after the rebuild (still idempotent, same as local).

## Cost reality

A Hostinger KVM plan is a flat monthly/annual fee (not usage-based like
Railway) — check current pricing at signup, but budget for the VPS
tier itself plus nothing else recurring (TLS is free via Let's
Encrypt, DNS is already included with the domain). Cheaper than
Railway's ~$10–20/mo at this workload's scale if the smallest KVM tier
is sufficient, at the cost of now owning OS patching, security updates,
and any future scaling decisions yourself.

## Explicitly deferred for this first deployment

Same list as `DEPLOYMENT.md`, plus two VPS-specific additions:

- Alembic migrations (schema stays on `Base.metadata.create_all()`)
- Rate limiting on login/API routes
- CSRF protection on mutating forms
- Structured logging / observability stack (`docker compose logs` only)
- Automated DB backups — **worth prioritizing sooner here than on
  Railway**, since there's no managed-Postgres automatic backup safety
  net at all; a simple `pg_dump` cron job is the natural first step
  when this gets picked up
- Unattended OS security updates (`unattended-upgrades` package) — not
  configured in this pass; worth enabling soon after, since Railway's
  managed containers made OS patching someone else's problem and this
  VPS makes it the user's
