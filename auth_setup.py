"""
auth_setup.py
=============

Run this ONCE to create your token, and again any time the cached token
expires (Schwab stops refreshing after ~7 days, at which point you must
re-authenticate through the browser).

    python auth_setup.py

What happens:
  1. A browser window opens to Schwab's login page.
  2. You log in and approve the app.
  3. Schwab redirects to your callback URL (https://127.0.0.1:8182).
     Your browser will warn about the self-signed certificate. That is
     expected for local development. Proceed past the warning.
  4. schwab-py captures the redirect and writes the token to disk.

The Streamlit dashboard (app.py) then reads this token file and never
needs an interactive login, until the token expires again.
"""

import os
import sys

from dotenv import load_dotenv
from schwab import auth


def main() -> None:
    load_dotenv()

    api_key = os.environ.get("SCHWAB_API_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "token.json")

    if not api_key or not app_secret:
        sys.exit(
            "Missing SCHWAB_API_KEY or SCHWAB_APP_SECRET. "
            "Copy .env.example to .env and fill in your credentials."
        )

    # easy_client runs the interactive login flow if the token file does
    # not exist, and simply loads it if it does. To force a fresh login
    # after expiry, delete the token file first (or this script will try
    # to reuse the stale one).
    if os.path.exists(token_path):
        print(f"A token already exists at '{token_path}'.")
        answer = input("Delete it and re-authenticate? [y/N] ").strip().lower()
        if answer == "y":
            os.remove(token_path)
        else:
            print("Keeping existing token. Nothing to do.")
            return

    print("Starting manual login flow...")
    auth.client_from_manual_flow(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=token_path,
    )
    print(f"Success. Token written to '{token_path}'.")


if __name__ == "__main__":
    main()
