"""
export_to_sheets.py
===================

Pull the current Schwab account data using the SAME authenticated
connection the dashboard uses (token.json), organize it into tables, and
write it to a Google Sheet. Your Schwab API keys stay here in Python and
never go into Google Apps Script.

Run with:
    python export_to_sheets.py

One-time setup is described in the README under "Google Sheets export":
you create a Google service account, download its JSON key, share your
sheet with the service account's email, and set GOOGLE_SHEET_ID and
GOOGLE_SERVICE_ACCOUNT_FILE in your .env.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import gspread
import pandas as pd
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread_dataframe import set_with_dataframe

import schwab_client as sc

load_dotenv()

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
ACCOUNT = os.environ.get("SCHWAB_ACCOUNT")  # optional: last 4 digits to pick one

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _gspread_client():
    if not SHEET_ID:
        raise SystemExit("Set GOOGLE_SHEET_ID in your .env (the long id from the sheet URL).")
    if not os.path.exists(SA_FILE):
        raise SystemExit(
            f"Service account file '{SA_FILE}' not found. "
            "See the README 'Google Sheets export' section."
        )
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def _pick_account():
    c = sc.get_client()
    accounts = sc.list_accounts(c)
    if not accounts:
        raise SystemExit("No linked accounts found.")
    if ACCOUNT:
        for a in accounts:
            if a["number"].endswith(ACCOUNT[-4:]):
                return c, a
    return c, accounts[0]


# Pad the Positions tab to at least this many data rows (blank rows kept
# present) so a pivot table over the range keeps a stable source size.
MIN_POSITION_ROWS = 200


def _pad_rows(df: pd.DataFrame, min_rows: int) -> pd.DataFrame:
    """Append blank rows so the frame has at least min_rows data rows.

    Blanks are empty strings, which write as empty cells and keep the
    existing numeric values intact (no int-to-float coercion).
    """
    if len(df) >= min_rows:
        return df
    pad = pd.DataFrame("", index=range(min_rows - len(df)), columns=df.columns)
    return pd.concat([df, pad], ignore_index=True)


def _build_frames(snap: dict) -> dict[str, pd.DataFrame]:
    liq = snap.get("liquidation_value") or 0
    denom = liq if liq and liq > 0 else 1

    # Positions detail (one row per position). The header is fixed and the
    # tab is padded to a minimum row count so a pivot built over the range
    # keeps working even when positions are few or none.
    position_columns = [
        "Symbol", "Ticker", "Price", "Category", "P/C", "Qty", "Avg price",
        "Premium", "Market value", "Day P&L", "Day P&L %", "Open P&L",
        "Open P&L %", "Alloc value", "Expiration", "Strike", "To Strike",
        "DTE", "Capital efficiency %", "Theta",
    ]
    positions = pd.DataFrame(snap.get("positions", []))
    if not positions.empty:
        positions = positions.rename(
            columns={
                "symbol": "Symbol", "ticker": "Ticker", "category": "Category",
                "underlying_price": "Price",
                "put_call": "P/C", "quantity": "Qty", "avg_price": "Avg price",
                "premium": "Premium",
                "market_value": "Market value", "day_pl": "Day P&L",
                "day_pl_pct": "Day P&L %", "open_pl": "Open P&L",
                "open_pl_pct": "Open P&L %", "theta_dollars": "Theta",
                "alloc_value": "Alloc value",
                "expiration": "Expiration", "strike": "Strike", "dte": "DTE",
                "capital_efficiency": "Capital efficiency %",
            }
        )
        # To Strike: how far the underlying sits above the strike, as a
        # percent of its price. CSP rows only (the cushion before assignment).
        price = pd.to_numeric(positions.get("Price"), errors="coerce")
        strike = pd.to_numeric(positions.get("Strike"), errors="coerce")
        is_csp = positions.get("Category") == "CSPs"
        valid = is_csp & price.notna() & strike.notna() & (price != 0)
        positions["To Strike"] = ((price - strike) / price * 100).where(valid)
    # Fix column set and order; missing columns come through empty.
    positions = positions.reindex(columns=position_columns)
    positions = _pad_rows(positions, MIN_POSITION_ROWS)

    # Allocation by category.
    alloc = pd.DataFrame(
        sorted(snap.get("allocation", {}).items(), key=lambda kv: kv[1], reverse=True),
        columns=["Category", "Value"],
    )
    if not alloc.empty:
        alloc["Percent of liq"] = (alloc["Value"] / denom * 100).round(1)

    # Allocation by ticker.
    byt = pd.DataFrame(
        sorted(snap.get("by_ticker", {}).items(), key=lambda kv: kv[1], reverse=True),
        columns=["Ticker", "Value"],
    )
    if not byt.empty:
        prices = snap.get("underlying_prices", {})
        byt.insert(1, "Price", byt["Ticker"].map(prices.get))
        byt["Percent of liq"] = (byt["Value"] / denom * 100).round(1)

    # Summary of balances and margin.
    position_total = sum(snap.get("allocation", {}).values())
    vix = snap.get("vix")
    reco = snap.get("vix_reco") or {}
    summary = pd.DataFrame(
        [
            ("Liquidation value", round(liq)),
            ("Cash", round(snap.get("cash") or 0)),
            ("Buying power", round(snap.get("buying_power") or 0)),
            ("Open P&L", round(snap.get("open_pl") or 0)),
            ("Day P&L", round(snap.get("day_pl") or 0)),
            ("Theta / day", round(snap.get("theta_total") or 0)),
            ("Theta annualized %", round(snap.get("theta_annual_pct"), 1)
             if snap.get("theta_annual_pct") is not None else ""),
            ("VIX", round(vix, 2) if vix is not None else ""),
            ("VIX regime", snap.get("vix_regime") or ""),
            ("Options buying power", round(snap.get("options_bp") or 0)
             if snap.get("options_bp") is not None else ""),
            ("Buying power %", round(reco["cash_pct"], 1) if reco else ""),
            ("VIX target", reco.get("target", "")),
            ("VIX recommendation", reco.get("action", "")),
            ("Position value (allocation)", round(position_total)),
            ("Margin in use", round(position_total - liq)),
        ],
        columns=["Measure", "Amount"],
    )

    # VIX Cash Allocation framework (Options Trading University). Current
    # band marked.
    active = sc.vix_regime(vix)
    vix_guide = pd.DataFrame(
        [
            {
                "VIX range": (f"{int(b['low'])}+" if b["high"] >= 1000
                              else f"<{int(b['high'])}" if b["low"] == 0
                              else f"{int(b['low'])}-{int(b['high'])}"),
                "Regime": b["regime"],
                "Cash": b["cash"],
                "Invested": b["invested"],
                "Note": b.get("note", ""),
                "Current": "<--" if (active and active == b["regime"]) else "",
            }
            for b in sc.VIX_GUIDE
        ]
    )

    # Theta by ticker (daily decay), with a total row.
    theta_items = sorted(
        snap.get("theta_by_ticker", {}).items(), key=lambda kv: kv[1], reverse=True
    )
    theta = pd.DataFrame(theta_items, columns=["Ticker", "Theta / day"])
    if not theta.empty:
        theta["Theta / day"] = theta["Theta / day"].round(2)
        total_row = pd.DataFrame(
            [{"Ticker": "Total", "Theta / day": round(snap.get("theta_total") or 0, 2)}]
        )
        theta = pd.concat([theta, total_row], ignore_index=True)

    # Pad the Summary tab to 200 rows so the timestamp at A15 and any pivots
    # have a stable, fully-active sheet.
    summary = _pad_rows(summary, MIN_POSITION_ROWS)

    return {"Summary": summary, "Positions": positions, "Allocation": alloc,
            "ByTicker": byt, "Theta": theta, "VIX Guide": vix_guide}


def _write(sh, title: str, df: pd.DataFrame) -> None:
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=title, rows=max(len(df) + 5, 20), cols=max(len(df.columns) + 2, 6)
        )
    ws.clear()
    set_with_dataframe(ws, df, resize=True)


def main() -> None:
    gc = _gspread_client()
    sh = gc.open_by_key(SHEET_ID)

    c, account = _pick_account()
    print(f"Pulling Schwab data for account {account['number']} ...")
    snap = sc.get_account_snapshot(c, account["hash"])

    frames = _build_frames(snap)
    for title, df in frames.items():
        if df is None or df.empty:
            print(f"Skipping '{title}' (no data).")
            continue
        print(f"Writing '{title}' ({len(df)} rows) ...")
        _write(sh, title, df)

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    try:
        sh.worksheet("Summary").update_acell("A15", f"Last updated: {stamp}")
    except Exception:
        pass

    print("Done. Google Sheet updated.")


if __name__ == "__main__":
    main()
