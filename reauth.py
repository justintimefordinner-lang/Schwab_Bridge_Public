"""
reauth.py
=========

App-driven, WRITE-ONLY Schwab OAuth for the dashboard.

The Trading Dashboard app never reads this bridge's secrets. It only ever
*writes* into this folder:

  * credentials.env          — your Schwab App Key + Secret (first-run setup)
  * reauth_inbox/start       — an empty marker: "please generate a login URL"
  * reauth_inbox/redirect_url — the pasted https://127.0.0.1:8182/?code=... URL

Everything that touches a secret happens HERE, inside the bridge, driven by
auto_push's loop:

  * generate the Schwab login URL          (start_auth)
  * exchange the pasted redirect URL       (finish_auth)  -> writes token.json
  * report progress back to the app        (_write_status)

Status flows back to the app ONE WAY only, through a sanitized
``schwab-auth.json`` written into the app's own data/ folder (the same diode
the dashboard already reads). No secret ever leaves the bridge.

State handling (option A): the login URL's CSRF ``state`` is generated here and
kept in a bridge-private ``.auth_state.json``, then paired with the pasted
redirect URL when we exchange it — so ``state`` is validated end to end.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from schwab import auth

# --- paths (all relative to the bridge's working directory) -----------------
CREDENTIALS_PATH = os.environ.get("SCHWAB_CREDENTIALS_PATH", "credentials.env")
TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "token.json")
CALLBACK_DEFAULT = "https://127.0.0.1:8182"

INBOX_DIR = "reauth_inbox"
START_MARKER = os.path.join(INBOX_DIR, "start")
REDIRECT_FILE = os.path.join(INBOX_DIR, "redirect_url")
STATE_FILE = ".auth_state.json"          # bridge-private; state + authorization_url
STATUS_FILE = "schwab-auth.json"         # written into APP_DATA_DIR (app reads it)

# A login URL is only good for a little while — expire a stale pending login so
# the UI doesn't offer a dead link forever.
AWAITING_TTL = 900  # seconds


# --- credential loading -----------------------------------------------------
def _load_creds() -> tuple[str | None, str | None, str]:
    """Read credentials fresh every time.

    Base config comes from .env (APP_DATA_DIR, callback, intervals); the App
    Key/Secret come from credentials.env, which the app writes and which wins
    if present. Reading fresh means a running bridge picks up first-run
    credentials the moment the app saves them — no restart needed.
    """
    load_dotenv(".env")
    load_dotenv(CREDENTIALS_PATH, override=True)
    return (
        os.environ.get("SCHWAB_API_KEY"),
        os.environ.get("SCHWAB_APP_SECRET"),
        os.environ.get("SCHWAB_CALLBACK_URL", CALLBACK_DEFAULT),
    )


def _chmod600(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best effort (e.g. non-POSIX filesystem)


def _token_writer(token_path: str):
    """Mirror schwab-py's own token writer: dump the (metadata-wrapped) token
    as JSON, then lock the file down."""
    def write(token, *args, **kwargs):
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(token, f)
        _chmod600(token_path)
    return write


# --- status diode (bridge -> app's data/ folder, one way) -------------------
def _status_path() -> str | None:
    # Prefer the freshly-loaded APP_DATA_DIR (set on first-run setup) over any
    # value research_sync may have cached at import time.
    data_dir = os.environ.get("APP_DATA_DIR")
    if not data_dir:
        try:
            from research_sync import _app_data_dir  # same resolver auto_push uses
            data_dir = _app_data_dir()
        except Exception:
            data_dir = None
    return os.path.join(data_dir, STATUS_FILE) if data_dir else None


_last_status: dict = {}


def _write_status(**fields) -> None:
    """Publish a sanitized status the app can read. Only rewrites when a
    meaningful field changes, so we don't churn the file every tick. NEVER put
    a secret in here — this file is readable by the app."""
    global _last_status
    status = {
        "configured": fields.get("configured", _last_status.get("configured", False)),
        "hasToken": os.path.exists(TOKEN_PATH),
        "authStatus": fields.get("authStatus", _last_status.get("authStatus", "idle")),
        "authorizationUrl": fields.get("authorizationUrl", _last_status.get("authorizationUrl")),
        "error": fields.get("error", None),
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    keys = ("configured", "hasToken", "authStatus", "authorizationUrl", "error")
    if _last_status and all(status[k] == _last_status.get(k) for k in keys):
        return
    _last_status = status
    path = _status_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except OSError:
        pass


def _clear_state() -> None:
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


# --- the two OAuth steps ----------------------------------------------------
def start_auth() -> str:
    """Generate a fresh Schwab login URL and persist its CSRF state. Returns
    the authorization URL (also stashed so finish_auth can validate state)."""
    api_key, app_secret, callback = _load_creds()
    if not api_key or not app_secret:
        raise RuntimeError("No credentials yet — save your App Key and Secret first.")

    ctx = auth.get_auth_context(api_key, callback)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "callback_url": ctx.callback_url,
                "state": ctx.state,
                "authorization_url": ctx.authorization_url,
                "created_at": time.time(),
            },
            f,
        )
    _chmod600(STATE_FILE)
    return ctx.authorization_url


def finish_auth(received_url: str) -> None:
    """Exchange the pasted redirect URL for a token (option A: the state from
    start_auth is paired with this URL and validated), then write token.json."""
    api_key, app_secret, _callback = _load_creds()
    if not api_key or not app_secret:
        raise RuntimeError("No credentials on file.")
    if not received_url or "code=" not in received_url:
        raise RuntimeError("That doesn't look like a Schwab redirect URL (no ?code=...).")
    if not os.path.exists(STATE_FILE):
        raise RuntimeError("No login in progress. Start the reconnection again.")

    with open(STATE_FILE, encoding="utf-8") as f:
        saved = json.load(f)

    ctx = auth.AuthContext(
        saved["callback_url"], saved.get("authorization_url", ""), saved["state"]
    )
    # Builds and returns a client as a side effect; we only need the token,
    # which token_write_func persists to disk.
    auth.client_from_received_url(
        api_key, app_secret, ctx, received_url.strip(), _token_writer(TOKEN_PATH)
    )
    _clear_state()


# --- called by auto_push ----------------------------------------------------
def _friendly(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "state" in low and "mismatch" in low:
        return "The login didn't match this request — start the reconnection again."
    if "invalid" in low and "grant" in low:
        return "Schwab rejected the login (code expired?). Try the whole flow again."
    return msg


def init_status() -> None:
    """Publish a baseline status at bridge startup so the app's Settings page
    reflects reality immediately."""
    api_key, app_secret, _ = _load_creds()
    configured = bool(api_key and app_secret)
    if os.path.exists(STATE_FILE):
        _clear_state()  # never resume a half-finished login across a restart
    base = "connected" if os.path.exists(TOKEN_PATH) else ("needs_login" if configured else "needs_setup")
    _write_status(configured=configured, authStatus=base, authorizationUrl=None, error=None)


def process_inbox() -> None:
    """Handle whatever the app has dropped in reauth_inbox/. Safe to call every
    loop tick; it only acts when a marker/file is present."""
    api_key, app_secret, _ = _load_creds()
    configured = bool(api_key and app_secret)

    # 1) "start" — the user asked for a fresh login URL.
    if os.path.exists(START_MARKER):
        try:
            os.remove(START_MARKER)
        except OSError:
            pass
        try:
            url = start_auth()
            _write_status(configured=True, authStatus="awaiting_login", authorizationUrl=url, error=None)
        except Exception as exc:  # noqa: BLE001 — surface it to the UI
            _write_status(configured=configured, authStatus="error", authorizationUrl=None, error=_friendly(exc))
        return

    # 2) "redirect_url" — the user pasted the post-login URL.
    if os.path.exists(REDIRECT_FILE):
        try:
            with open(REDIRECT_FILE, encoding="utf-8") as f:
                received = f.read().strip()
        except OSError:
            received = ""
        try:
            os.remove(REDIRECT_FILE)
        except OSError:
            pass
        try:
            finish_auth(received)
            _write_status(configured=True, authStatus="connected", authorizationUrl=None, error=None)
        except Exception as exc:  # noqa: BLE001
            _write_status(configured=True, authStatus="error", authorizationUrl=None, error=_friendly(exc))
        return

    # 3) idle — expire a stale pending login; otherwise just keep
    #    configured/hasToken current (the change-detector avoids churn).
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if time.time() - saved.get("created_at", 0) > AWAITING_TTL:
                _clear_state()
                _write_status(configured=configured, authStatus="needs_login",
                              authorizationUrl=None, error="Login link expired — start again.")
            else:
                _write_status(configured=configured, authStatus="awaiting_login",
                              authorizationUrl=saved.get("authorization_url"))
        except OSError:
            _write_status(configured=configured)
    else:
        _write_status(configured=configured)


def note_app_pull(ok: bool, err: Exception | None = None) -> None:
    """Called by auto_push after the main 'app' export runs, so the connected /
    needs-login badge stays honest without any extra API calls. Never stomps an
    in-progress login."""
    if os.path.exists(STATE_FILE) or os.path.exists(START_MARKER) or os.path.exists(REDIRECT_FILE):
        return
    if ok:
        _write_status(configured=True, authStatus="connected", authorizationUrl=None, error=None)
        return
    msg = (str(err) or "") if err else ""
    low = msg.lower()
    looks_auth = (
        (err is not None and err.__class__.__name__ == "AuthError")
        or "invalid_grant" in low
        or "token" in low
        or "expired" in low
        or "re-authenticate" in low
    )
    if looks_auth:
        _write_status(configured=True, authStatus="needs_login",
                      authorizationUrl=None, error="Token expired — reconnect Schwab.")
