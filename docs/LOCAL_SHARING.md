# Sharing this app with someone else — without cloud hosting

Exposes the app running on **your own machine** to one specific person
over the internet, without deploying anywhere. Two independent layers
of access control:

1. **Cloudflare Access** — blocks everyone except the exact email
   addresses you allow-list, before the request ever reaches your
   machine.
2. **This app's own login** — same as always (see
   [`OPERATIONS_RUNBOOK.md`](OPERATIONS_RUNBOOK.md) section 5 for
   creating your friend a proper scoped account instead of sharing
   yours).

Only the dashboard (port 8123) is ever exposed — nothing else on your
machine becomes reachable. No router configuration or firewall changes
needed: `cloudflared` only makes outbound connections to Cloudflare, so
there's no inbound port to open.

## Prerequisite: a domain in your Cloudflare account

Cloudflare Access policies attach to a real hostname in a domain you
manage through Cloudflare — this is the one real requirement, and it's
needed either way:

- **If you already manage a domain through Cloudflare** (e.g. if
  `joyjuice.in` is already a Cloudflare zone), you can use a subdomain
  of it, e.g. `equipment.joyjuice.in`, without buying anything new.
- **If not**, add any domain you already own to Cloudflare (Cloudflare
  dashboard → "Add a site" → follow the nameserver-change instructions
  — free, takes a few minutes to propagate), or register a cheap one
  (~$10–15/yr) through Cloudflare Registrar or elsewhere and add it the
  same way.

You do **not** need to point the domain at anything else or use it for
email/a website — it just needs to exist as a zone in your account.

## Step 1 — Install `cloudflared`

```powershell
winget install --id Cloudflare.cloudflared
```
(Or download the Windows binary directly from
[Cloudflare's GitHub releases](https://github.com/cloudflare/cloudflared/releases)
if `winget` isn't available.)

## Step 2 — Authenticate `cloudflared` to your Cloudflare account

```bash
cloudflared tunnel login
```
Opens a browser — log in and pick the domain (zone) from Step 0. This
downloads a certificate `cloudflared` uses to manage tunnels/DNS for
that zone on your behalf.

## Step 3 — Create a named tunnel

```bash
cloudflared tunnel create equipment-dashboard
```
Prints a tunnel ID and writes a credentials file (something like
`C:\Users\hp\.cloudflared\<tunnel-id>.json`). **Treat this file as a
secret** — it's what lets `cloudflared` authenticate as this tunnel;
don't commit it to the repo (it isn't inside the project folder by
default, so this should already be a non-issue).

## Step 4 — Point a hostname at the tunnel

```bash
cloudflared tunnel route dns equipment-dashboard equipment.yourdomain.com
```
Creates the DNS record automatically in your Cloudflare zone. Replace
`equipment.yourdomain.com` with whatever subdomain you want.

## Step 5 — Configure the tunnel to forward to your local dashboard

Create `C:\Users\hp\.cloudflared\config.yml`:
```yaml
tunnel: equipment-dashboard
credentials-file: C:\Users\hp\.cloudflared\<tunnel-id>.json

ingress:
  - hostname: equipment.yourdomain.com
    service: http://127.0.0.1:8123
  - service: http_status:404
```
This is what actually enforces "only this application is exposed" —
the tunnel only knows how to forward the one hostname to the one local
port (8123, the dashboard). Anything else is caught by the final
`http_status:404` rule and never forwarded anywhere. No other service,
port, or file on your machine is reachable through this tunnel, by
construction.

## Step 6 — Run the tunnel

```bash
cloudflared tunnel run equipment-dashboard
```
Leave this running alongside the app's own two processes (see
`OPERATIONS_RUNBOOK.md` section 1) — it's a third long-lived process.
**To persist across a reboot**, install it as a Windows service instead
of running it manually:
```powershell
cloudflared service install
```
Then it starts automatically with Windows, same as any other Windows
service (`services.msc`).

At this point `https://equipment.yourdomain.com` reaches your local
dashboard — but so would anyone else who guesses or finds the URL.
Step 7 is what actually restricts it.

## Step 7 — Lock it down with Cloudflare Access

1. Go to the Zero Trust dashboard: <https://one.dash.cloudflare.com>
   (same account as your regular Cloudflare login — Zero Trust has a
   generous free tier, plenty for one or two users).
2. **Access → Applications → Add an application → Self-hosted.**
3. **Application domain**: `equipment.yourdomain.com`.
4. **Add a policy** — Action: `Allow`. Under "Include", choose
   **Emails** and list the exact addresses allowed in: your friend's,
   and your own (so you don't lock yourself out).
5. Save. Pick a session duration (e.g. 24 hours) so they're not
   re-verifying constantly.

From now on, hitting the URL first shows **Cloudflare's own
verification page** — the visitor enters their email, gets a one-time
code sent to it, and only then is the request forwarded to your
machine at all. Anyone not on the allow-list is stopped at Cloudflare's
edge and never reaches your app, your network, or your machine.

## Step 8 — Give your friend their own account in the app

Cloudflare Access only proves **who** they are — it says nothing about
**what** they should see inside the app. Don't hand them your own
admin login. Instead, create them a dedicated account with an
appropriately limited role via `/users` (or the CLI snippet) — see
[`OPERATIONS_RUNBOOK.md`](OPERATIONS_RUNBOOK.md#5-checking-or-modifying-user-database-and-access-rights):
`operations` if they should only see equipment monitoring, or
`venue_partner` (scoped to one venue) if it's order data they're after.
Send them their own dashboard login credentials separately from the
Cloudflare Access step.

## Verifying it actually works

- Open the URL yourself in an **incognito/private window** (so you're
  not already Cloudflare-authenticated) — you should hit Cloudflare's
  verification page first, then land on this app's own `/login` page
  only after verifying.
- Have your friend do the same with their allow-listed email.
- Try a third email that isn't on the list — confirm Cloudflare shows
  "Access Denied" and the app's login page is never reached.

## Notes

- If this machine sleeps or loses power, the tunnel (and the app
  itself) goes down — same pre-existing limitation as running the app
  locally at all (see `README.md`). This doesn't fix that; only a real
  cloud deployment (`DEPLOYMENT.md`) does.
- Removing access later: delete the email from the Access policy (Step
  7) — takes effect immediately, no need to touch the tunnel or the app.
- Shutting the whole thing down: `cloudflared tunnel run` → Ctrl+C (or
  stop the Windows service), then optionally `cloudflared tunnel delete
  equipment-dashboard` to remove it entirely.
