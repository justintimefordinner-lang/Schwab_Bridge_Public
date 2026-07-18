# Schwab Trading Bridge (read-only)

A small Python bridge that pulls balances, positions, options, and order
history from a Charles Schwab brokerage account and writes them as JSON for a
dashboard front-end to render. **Read-only: it never places or cancels trades.**

`auto_push.py` runs on a loop (default every 60s) and keeps the dashboard's
`data/` folder current. It's built to run continuously —
for example as a `systemd` service on a Raspberry Pi.

## One-time setup

### 1. Register a Schwab developer app (do this first — approval takes a few days)

1. Go to https://developer.schwab.com/dashboard/apps and create an app.
2. Add the **Accounts and Trading Production** API product (add
   **Market Data Production** too if you want live quotes).
3. Set the callback URL to `https://127.0.0.1:8182`.
4. Submit. Approval moves from "Approved - Pending" to "Ready for Use" after a
   few days. You cannot authenticate until it is ready.
5. Copy the **App Key** and **Secret** from the app page.

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> On Windows PowerShell, script execution may be disabled — if `activate`
> errors, just call the venv's Python directly: `.venv\Scripts\python.exe <script>.py`.

### 3. Configure

```bash
cp .env.example .env
```

Then edit `.env`:
- Set **`APP_DATA_DIR`** to the absolute path of your dashboard app's `data/`
  folder (this is where the JSON snapshots are written).

Your Schwab **App Key** and **Secret** do *not* go in `.env`. Either set them
once from the dashboard (see [Connect from the dashboard](#connect-from-the-dashboard)
below — recommended), or copy `credentials.env.example` → `credentials.env` and
paste them there.

### 4. Authenticate

The easiest way is from the dashboard (see below — it also works headless and
from your phone). To do it from the CLI instead:

```bash
python auth_setup.py
```

A browser opens to Schwab's login. Approve the app. Your browser will warn
about the self-signed certificate on `127.0.0.1` — that's expected locally,
proceed past it. A `token.json` file is written on success.

## Run

```bash
python auto_push.py
```

This runs the push loop that feeds the dashboard's `data/` folder. To run it
24/7, wrap it in a service manager
(`systemd`, `pm2`, etc.) pointed at `.venv/bin/python auto_push.py` with this
folder as the working directory.

## Connect from the dashboard

The dashboard app can handle both first-run setup and the weekly re-login for
you — no CLI, and it works headless / from your phone. The app is **write-only**
toward this bridge: it *deposits* your App Key/Secret and the pasted login URL
into this folder and never reads the bridge's secrets back.

Open the app's **Settings → Schwab connection** and:

1. **First run:** paste your **App Key** and **App Secret**. The app writes them
   to `credentials.env` (chmod 600) and asks the bridge for a login link.
2. Click **Log in to Schwab**, approve access, then copy the whole address you
   land on (`https://127.0.0.1:8182/?code=…` — the page won't load; that's
   expected).
3. Paste that URL back and **Finish**. The bridge exchanges it, writes
   `token.json`, and live data resumes within a cycle.

Under the hood, `reauth.py` (driven by the `auto_push` loop) watches
`reauth_inbox/`, generates the login URL with `get_auth_context`, and completes
the exchange with `client_from_received_url` — the OAuth `state` is generated
and validated on the bridge, end to end. Progress is reported back one-way
through a sanitized `schwab-auth.json` in the app's `data/` folder (no secrets).

## When it stops working after ~7 days

Schwab refuses to refresh the token after about seven days. `auto_push.py` will
log `invalid_grant` errors. The easiest fix is **Settings → Schwab connection →
Reconnect** in the dashboard (above). From the CLI instead:

```bash
python auth_setup.py          # re-run the browser login, rewrites token.json
```

(If running headless without the dashboard, authenticate on a machine with a
browser and copy the fresh `token.json` over, then restart the service.)

## Files

| File                    | Role                                                     |
|-------------------------|----------------------------------------------------------|
| `auth_setup.py`         | Interactive CLI Schwab login; writes `token.json`        |
| `reauth.py`             | App-driven login/exchange (dashboard Settings → Connect) |
| `auto_push.py`          | Main loop — pushes data + services the re-auth inbox     |
| `schwab_client.py`      | Read-only Schwab data layer (balances, positions, orders)|
| `export_to_app.py`      | Writes the dashboard JSON snapshot                       |
| `sync_trade_history.py` | Builds trade / transaction history                       |
| `app.py`                | Optional standalone Streamlit view                       |

## Security notes

- **Never commit** `token.json`, `.env`, or `credentials.env` — they hold live
  API credentials. They're already in `.gitignore`; keep them there.
- Your App Key/Secret live in `credentials.env` (written by the dashboard, or by
  hand from `credentials.env.example`). The dashboard is **write-only** toward
  this folder — it deposits credentials and the pasted login URL and never reads
  them back; `.env` holds only non-secret config and refresh intervals.
- Copy `.env.example` → `.env` for that config. The `.example` files are
  placeholders and safe to commit.
- **Commit-time secret guard:** a dependency-free `.githooks/pre-commit` (plus a
  gitleaks `.pre-commit-config.yaml`) blocks accidental commits of credential
  files or secret-looking values. After cloning, turn it on with
  `git config core.hooksPath .githooks`.
- Trade execution is intentionally **not** in this codebase. If you add it
  later, keep it in a separate module so this read-only surface stays small.
