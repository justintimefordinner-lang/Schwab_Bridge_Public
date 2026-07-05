# Schwab account dashboard (read-only)

A local Streamlit dashboard that displays balances, positions, and recent
order status for a Charles Schwab trading account. Read-only: it never
places or cancels trades.

## One-time setup

### 1. Register a developer app (do this first, it takes a few days)

1. Go to https://developer.schwab.com/dashboard/apps and create an app.
2. Add the **Accounts and Trading Production** API product (add
   **Market Data Production** too if you later want quotes).
3. Set the callback URL to `https://127.0.0.1:8182`.
4. Submit. Approval moves from "Approved - Pending" to "Ready for Use"
   after a few days. You cannot authenticate until it is ready.
5. Copy the **App Key** and **Secret** from the app page.

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env and paste in your App Key and Secret
```

### 4. Authenticate

```bash
python auth_setup.py
```

A browser opens to Schwab's login. Approve the app. Your browser will
warn about the self-signed certificate on `127.0.0.1`; that is expected
locally, proceed past it. A `token.json` file is written on success.

## Run the dashboard

```bash
streamlit run app.py
```

## When it stops working after ~7 days

Schwab refuses to refresh the token after about seven days. The
dashboard will show an auth error. Fix it by re-authenticating:

```bash
python auth_setup.py        # choose "y" to delete and recreate
```

## Files

| File               | Role                                              |
|--------------------|---------------------------------------------------|
| `auth_setup.py`    | One-time / weekly interactive login, writes token |
| `schwab_client.py` | Read-only data layer (balances, positions, orders)|
| `app.py`           | Streamlit dashboard UI                            |

## Security notes

- `token.json` and `.env` contain secrets. Never commit them. Add both
  to `.gitignore`.
- Trade execution is intentionally NOT in this codebase. If you add it
  later, keep it in a separate module so this read-only surface stays
  small.
