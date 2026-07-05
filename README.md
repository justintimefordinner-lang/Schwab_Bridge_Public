# Schwab Trading Bridge (read-only)

A small Python bridge that pulls balances, positions, options, and order
history from a Charles Schwab brokerage account and writes them as JSON for a
dashboard front-end to render — and optionally mirrors the same data to a
Google Sheet. **Read-only: it never places or cancels trades.**

`auto_push.py` runs on a loop (default every 60s) and keeps the dashboard's
`data/` folder and your Google Sheet current. It's built to run continuously —
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
- Paste your Schwab **App Key** and **Secret**.
- Set **`APP_DATA_DIR`** to the absolute path of your dashboard app's `data/`
  folder (this is where the JSON snapshots are written).

### 4. (Optional) Google Sheets mirror

Only needed if you want `export_to_sheets.py` to update a Google Sheet:

1. In Google Cloud, create a **service account** and enable the **Google Sheets API**.
2. Download its JSON key, save it as `service_account.json`
   (see `service_account.example.json` for the expected shape).
3. **Share your target Sheet** with the service account's `client_email`.
4. Put the Sheet's ID in `GOOGLE_SHEET_ID` in `.env`.

Skip this entirely if you don't use Sheets — the dashboard push works without it.

### 5. Authenticate

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

This runs the push loop that feeds the dashboard's `data/` folder (and the
Google Sheet, if configured). To run it 24/7, wrap it in a service manager
(`systemd`, `pm2`, etc.) pointed at `.venv/bin/python auto_push.py` with this
folder as the working directory.

## When it stops working after ~7 days

Schwab refuses to refresh the token after about seven days. `auto_push.py` will
log `invalid_grant` errors. Fix it by re-authenticating and restarting:

```bash
python auth_setup.py          # re-run the browser login, rewrites token.json
```

(If running headless, authenticate on a machine with a browser and copy the
fresh `token.json` over, then restart the service.)

## Files

| File                    | Role                                                     |
|-------------------------|----------------------------------------------------------|
| `auth_setup.py`         | Interactive Schwab login; writes `token.json`            |
| `auto_push.py`          | Main loop — pushes data to the dashboard + Google Sheets |
| `schwab_client.py`      | Read-only Schwab data layer (balances, positions, orders)|
| `export_to_app.py`      | Writes the dashboard JSON snapshot                       |
| `export_to_sheets.py`   | Writes the Google Sheet                                  |
| `sync_trade_history.py` | Builds trade / transaction history                       |
| `app.py`                | Optional standalone Streamlit view                       |

## Security notes

- **Never commit** `token.json`, `.env`, or `service_account.json` — they hold
  live API credentials. They're already in `.gitignore`; keep them there.
- Copy `.env.example` → `.env` and `service_account.example.json` →
  `service_account.json` and fill in your own values. The `.example` files are
  placeholders and safe to commit.
- Trade execution is intentionally **not** in this codebase. If you add it
  later, keep it in a separate module so this read-only surface stays small.
