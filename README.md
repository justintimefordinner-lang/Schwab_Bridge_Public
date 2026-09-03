# Schwab Trading Bridge (read-only)

A small Python bridge that pulls balances, positions, options, and order
history from a Charles Schwab brokerage account and writes them as JSON for a
dashboard front-end to render. **Read-only: it never places or cancels trades.**

`auto_push.py` runs on a loop (default every 60s) and keeps the dashboard's
`data/` folder current. It's built to run continuously — normally as a Docker
container beside the dashboard, on a Raspberry Pi, a PC, or a Mac.

> **Step-by-step walkthrough** (from a blank Raspberry Pi SD card to live data):
> [Portfolio Manager setup guide](https://claude.ai/code/artifact/9eea9386-6f02-41fb-ba82-629ee9c95ad7).

## Run with Docker (recommended)

The dashboard repo's `docker-compose.yml` starts both halves together. This
repo ships the `Dockerfile` for the bridge half: a **dependencies-only** image
(Python 3.12, `requirements.txt` minus `streamlit`). Your checkout is
bind-mounted into the container at `/bridge`, so `.env`, `credentials.env`,
`token.json`, `reauth_inbox/` and `task_inbox/` live in this folder exactly as
they would bare-metal — nothing secret is ever baked into an image.

1. **Register a Schwab developer app** (next section — start it first, approval
   takes days).
2. **Clone side by side.** The folder must be named `schwab-bridge` (or set
   `BRIDGE_DIR` in the dashboard's `.env`):
   ```bash
   mkdir -p ~/portfolio && cd ~/portfolio
   git clone https://github.com/justintimefordinner-lang/Schwab_Bridge_Public.git schwab-bridge
   git clone https://github.com/justintimefordinner-lang/Trading_Dashboard_App.git
   ```
3. **Start from the dashboard folder** — install steps, `.env` settings, and the
   Settings-page login are all in the
   [dashboard README](https://github.com/justintimefordinner-lang/Trading_Dashboard_App#quick-start-with-docker-recommended):
   ```bash
   cd ~/portfolio/Trading_Dashboard_App
   cp .env.docker.example .env
   docker compose up -d --build
   ```
4. **Connect Schwab from the dashboard's Settings page** (see
   [Connect from the dashboard](#connect-from-the-dashboard)). That writes
   `credentials.env` and this bridge's `.env` for you — in a container
   `APP_DATA_DIR` is `/app/data`, the path the dashboard's `data/` folder is
   mounted at in *both* containers, so don't hand-edit it to a host path.

Things worth knowing:

- **Before Settings has been saved once, the container restarts in a loop**
  printing "Set APP_DATA_DIR". That's expected; it settles the moment the
  dashboard writes `.env`.
- **Updating:** the code is bind-mounted, so `git pull` then
  `docker compose restart bridge` (from the dashboard folder) is enough. Only a
  change to `requirements.txt` needs `docker compose up -d --build bridge`.
- **Logs:** `docker compose logs -f bridge`.
- **Runs as you**, not root (`UID`/`GID` in the dashboard's `.env`), so
  `token.json` and everything in `data/` stay editable from your own shell.
- **Second Schwab login:** a second clone at `../schwab-bridge-acct2` writing
  `data/acct2/` is supported via the dashboard's `docker-compose.acct2.yml`.
- `app.py` (the standalone Streamlit view) is *not* in the image; run it
  bare-metal if you want it.

## 1. Register a Schwab developer app (do this first — approval takes a few days)

1. Go to https://developer.schwab.com/dashboard/apps and create an
   **Individual Developer** account (a different login from your brokerage) and
   an app.
2. Add the **Accounts and Trading Production** API product (add
   **Market Data Production** too if you want live quotes).
3. Set the callback URL to exactly `https://127.0.0.1:8182` — no trailing slash.
4. Submit. Approval moves from "Approved - Pending" to "Ready for Use" after a
   few days. You cannot authenticate until it is ready.
5. Copy the **App Key** and **Secret** from the app page. You'll paste them into
   the dashboard's Settings page — not into any file.

> **About that callback URL:** with the dashboard flow, nothing ever listens at
> `127.0.0.1:8182`. Schwab just needs a registered address to bounce you back
> to after login; you copy that address out of your browser's bar by hand. That
> is why setup works on a Pi with no screen, and why the "can't reach this
> page" error you'll see during login is completely expected.

## Connect from the dashboard

The dashboard app handles both first-run setup and the weekly re-login for
you — no CLI, and it works headless / from your phone. The app is **write-only**
toward this bridge: it *deposits* your App Key/Secret and the pasted login URL
into this folder and never reads the bridge's secrets back.

Open the app's **Settings → Schwab connection** and:

1. **First run:** paste your **App Key** and **App Secret**. The app writes them
   to `credentials.env` (chmod 600) and asks the bridge for a login link.
2. Click **Save & start login**, open the link, log in with your *brokerage*
   credentials and approve access, then copy the whole address you land on
   (`https://127.0.0.1:8182/?code=…` — the page won't load; that's expected).
3. Paste that URL back and **Finish sign-in**. The bridge exchanges it, writes
   `token.json`, and live data resumes within a cycle.

Under the hood, `reauth.py` (driven by the `auto_push` loop) watches
`reauth_inbox/`, generates the login URL with `get_auth_context`, and completes
the exchange with `client_from_received_url` — the OAuth `state` is generated
and validated on the bridge, end to end. Progress is reported back one-way
through a sanitized `schwab-auth.json` in the app's `data/` folder (no secrets).

## When it stops working after ~7 days — make reconnecting a Sunday habit

Schwab refuses to refresh the token after about seven days. `auto_push.py` will
log `invalid_grant` errors and the dashboard's Settings card will say
"Not connected — reconnect to resume live data." Click **Reconnect Schwab** and
repeat the link-and-paste; it takes under a minute and works from your phone.

Seven days is the outside limit, so reconnecting every **Sunday** means the
token never expires during market hours. From the CLI instead (bare-metal only):

```bash
python auth_setup.py          # re-run the browser login, rewrites token.json
```

## Running without Docker

### Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> On Windows PowerShell, script execution may be disabled — if `activate`
> errors, just call the venv's Python directly: `.venv\Scripts\python.exe <script>.py`.

### Configure

```bash
cp .env.example .env
```

Then edit `.env` and set **`APP_DATA_DIR`** to the absolute path of your
dashboard app's `data/` folder (this is where the JSON snapshots are written).

Your Schwab **App Key** and **Secret** do *not* go in `.env`. Either set them
once from the dashboard (recommended — see above), or copy
`credentials.env.example` → `credentials.env` and paste them there.

### Authenticate

The easiest way is from the dashboard (above). To do it from the CLI instead:

```bash
python auth_setup.py
```

A browser opens to Schwab's login. Approve the app. Your browser will warn
about the self-signed certificate on `127.0.0.1` — that's expected locally,
proceed past it. A `token.json` file is written on success.

### Run

```bash
python auto_push.py
```

This runs the push loop that feeds the dashboard's `data/` folder. To run it
24/7 without Docker, wrap it in a service manager (`systemd`, `pm2`, etc.)
pointed at `.venv/bin/python auto_push.py` with this folder as the working
directory.

## Files

| File                    | Role                                                     |
|-------------------------|----------------------------------------------------------|
| `Dockerfile`            | Dependencies-only image; code is bind-mounted at runtime |
| `.dockerignore`         | Keeps secrets, state, and `data` out of any image layer  |
| `auth_setup.py`         | Interactive CLI Schwab login; writes `token.json`        |
| `reauth.py`             | App-driven login/exchange (dashboard Settings → Connect) |
| `auto_push.py`          | Main loop — pushes data + services the re-auth inbox     |
| `schwab_client.py`      | Read-only Schwab data layer (balances, positions, orders)|
| `export_to_app.py`      | Writes the dashboard JSON snapshot                       |
| `sync_trade_history.py` | Builds trade / transaction history                       |
| `s5fi_breadth.py`       | S&P 500 breadth via yfinance (Schwab doesn't serve it)   |
| `app.py`                | Optional standalone Streamlit view (not in the image)    |

## Security notes

- **Never commit** `token.json`, `.env`, or `credentials.env` — they hold live
  API credentials. They're already in `.gitignore` and `.dockerignore`; keep
  them there.
- Your App Key/Secret live in `credentials.env` (written by the dashboard, or by
  hand from `credentials.env.example`). The dashboard is **write-only** toward
  this folder — it deposits credentials and the pasted login URL and never reads
  them back; `.env` holds only non-secret config and refresh intervals.
- Copy `.env.example` → `.env` for that config. The `.example` files are
  placeholders and safe to commit.
- The container runs as your own user, and the bridge only ever *reads* from
  Schwab — there is no code in it that can place an order.
- **Commit-time secret guard:** a dependency-free `.githooks/pre-commit` (plus a
  gitleaks `.pre-commit-config.yaml`) blocks accidental commits of credential
  files or secret-looking values. After cloning, turn it on with
  `git config core.hooksPath .githooks`.
- Trade execution is intentionally **not** in this codebase. If you add it
  later, keep it in a separate module so this read-only surface stays small.
