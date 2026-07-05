#!/usr/bin/env python3
"""
fetch_earnings.py — populate data/earnings.json with next-earnings dates.

Schwab's market-data API does not expose earnings dates, so the AM report reads them
from a small file: data/earnings.json = {"AMD": "2026-07-29", ...}. This optional
helper fills that file from Yahoo via yfinance. It is best-effort and unofficial — if
it can't reach a symbol it leaves any existing date in place rather than wiping it, so
a hand-maintained file degrades gracefully. The main report never imports this; it only
ever reads the JSON, so a yfinance breakage can't affect the briefing.

    pip install yfinance
    python fetch_earnings.py          # refresh all approved names
    python fetch_earnings.py AMD MU   # refresh just these

If you'd rather maintain dates by hand (or paste from another source), just edit
data/earnings.json directly — the format is {"TICKER": "YYYY-MM-DD"}.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime

from dotenv import load_dotenv

from research_sync import _app_data_dir, load_approved

load_dotenv()   # pull APP_DATA_DIR (and friends) from .env, like the other scripts

EARNINGS_FILE = "earnings.json"


def _next_earnings(sym: str):
    """Next future earnings date for `sym` as 'YYYY-MM-DD', or None."""
    import yfinance as yf

    t = yf.Ticker(sym)
    candidates: list[date] = []

    # newer yfinance: .calendar is a dict with an 'Earnings Date' list
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            for d in cal.get("Earnings Date", []) or []:
                candidates.append(d if isinstance(d, date) else datetime.fromisoformat(str(d)).date())
    except Exception:
        pass

    # fallback: the earnings-dates table (past + upcoming)
    if not candidates:
        try:
            df = t.get_earnings_dates(limit=12)
            if df is not None:
                for idx in df.index:
                    d = idx.date() if hasattr(idx, "date") else None
                    if d:
                        candidates.append(d)
        except Exception:
            pass

    today = date.today()
    future = sorted(d for d in candidates if d >= today)
    return future[0].isoformat() if future else None


def main(argv: list[str]) -> None:
    data_dir = _app_data_dir()
    path = os.path.join(data_dir, EARNINGS_FILE)

    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}

    symbols = [s.upper() for s in argv[1:]] or load_approved(data_dir)
    updated, missed = 0, []
    for sym in symbols:
        try:
            d = _next_earnings(sym)
        except ModuleNotFoundError:
            print("yfinance not installed — run: pip install yfinance")
            return
        except Exception:
            d = None
        if d:
            existing[sym] = d
            updated += 1
        else:
            missed.append(sym)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(existing.items())), f, indent=2)

    print("Wrote %s  (%d updated, %d unchanged/missing)" % (path, updated, len(missed)))
    if missed:
        print("  no date found for:", ", ".join(missed))


if __name__ == "__main__":
    main(sys.argv)
