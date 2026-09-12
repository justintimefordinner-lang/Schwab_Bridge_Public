"""Sector / industry lookup for the dashboard's Portfolio Risk screen.

Schwab's Trader API carries no sector classification: its instruments
"fundamental" projection has valuation ratios, market cap and beta, nothing
GICS-like. So this comes from Yahoo Finance via yfinance (already a dependency
here for earnings dates and S5FI breadth) and is cached in data/sectors.json.

Sectors change roughly never, so a ticker is looked up once and refreshed only
after MAX_AGE_DAYS. At most MAX_LOOKUPS_PER_RUN cold lookups run per export,
so the one-minute snapshot cycle never stalls on Yahoo; a fresh portfolio fills
in over a few cycles. A ticker Yahoo can't classify is remembered as such (with
a shorter TTL) so it isn't retried every minute.

The dashboard applies the file's `overrides` map on top of the looked-up
values, so a misclassification can be corrected by hand and this module never
touches that map. File shape (data/sectors.json):

    {
      "asof": "2026-09-11T14:02:11+00:00",
      "tickers": {
        "AAPL": {"sector": "Technology", "industry": "Consumer Electronics",
                 "quoteType": "EQUITY", "asof": "2026-09-11T14:02:11+00:00"},
        "SMH":  {"sector": "ETF / Index Fund", "industry": null,
                 "quoteType": "ETF", "asof": "..."}
      },
      "overrides": {"XYZ": "Industrials"}
    }

Standalone check (uses APP_DATA_DIR from .env, or the current folder):

    python sectors.py AAPL SMH SWVXX
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

SECTORS_FILE = "sectors.json"
MAX_AGE_DAYS = 90          # re-check a classified ticker this often
UNKNOWN_AGE_DAYS = 7       # retry a ticker Yahoo couldn't classify this often
MAX_LOOKUPS_PER_RUN = 8    # cap cold lookups per export so the cycle stays quick

# Non-equity quote types get a fixed bucket instead of a Yahoo sector. The
# dashboard already excludes money-market sweeps from capital-per-ticker, so the
# fund bucket mostly matters for a held bond/commodity fund.
_FIXED_BUCKETS = {
    "ETF": "ETF / Index Fund",
    "MUTUALFUND": "Fund",
    "MONEYMARKET": "Cash & Equivalents",
    "INDEX": "Index",
    "CRYPTOCURRENCY": "Crypto",
    "FUTURE": "Futures",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_days(asof: str | None) -> float:
    if not asof:
        return float("inf")
    try:
        then = datetime.fromisoformat(asof)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds() / 86400
    except (TypeError, ValueError):
        return float("inf")


def yf_symbol(sym: str) -> str:
    """Schwab's BRK.B is Yahoo's BRK-B."""
    return sym.strip().upper().replace(".", "-")


def is_lookup_worthy(sym: str) -> bool:
    """Skip index symbols ($SPX), futures (/MES), money-market sweeps (SWVXX)
    and anything that isn't a plain ticker."""
    s = (sym or "").strip().upper()
    if not s or s.startswith(("$", "/")):
        return False
    if re.fullmatch(r"[A-Z]{3}XX", s):
        return False
    return re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) is not None


def classify(info: dict[str, Any]) -> dict[str, Any]:
    """Reduce a yfinance info dict to {sector, industry, quoteType}. sector is
    None when Yahoo has no classification (the dashboard shows those as
    Unclassified rather than dropping them)."""
    qt = str(info.get("quoteType") or "").upper()
    if qt in _FIXED_BUCKETS:
        return {"sector": _FIXED_BUCKETS[qt], "industry": None, "quoteType": qt}
    sector = info.get("sector") or info.get("sectorDisp") or None
    industry = info.get("industry") or info.get("industryDisp") or None
    return {
        "sector": str(sector).strip() if sector else None,
        "industry": str(industry).strip() if industry else None,
        "quoteType": qt or "UNKNOWN",
    }


def lookup_sector(sym: str) -> dict[str, Any] | None:
    """One Yahoo lookup. Returns the classify() dict, or None when the call
    itself failed (network, rate limit) so the caller can leave the cache alone."""
    try:
        import yfinance as yf
    except ImportError:
        print("  note: yfinance not installed — run: pip install yfinance")
        return None
    try:
        t = yf.Ticker(yf_symbol(sym))
        info = None
        get_info = getattr(t, "get_info", None)
        if callable(get_info):
            try:
                info = get_info()
            except Exception:
                info = None
        if not info:
            info = t.info
        if not isinstance(info, dict) or not info:
            return None
        return classify(info)
    except Exception as exc:
        print(f"  note: sector lookup failed for {sym} ({exc}).")
        return None


def load_sectors(data_dir: str) -> dict[str, Any]:
    path = os.path.join(data_dir, SECTORS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        raw = {}
    tickers = raw.get("tickers") if isinstance(raw, dict) else None
    overrides = raw.get("overrides") if isinstance(raw, dict) else None
    return {
        "asof": raw.get("asof") if isinstance(raw, dict) else None,
        "tickers": tickers if isinstance(tickers, dict) else {},
        "overrides": overrides if isinstance(overrides, dict) else {},
    }


def _save(data_dir: str, store: dict[str, Any]) -> None:
    path = os.path.join(data_dir, SECTORS_FILE)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"  note: could not write {SECTORS_FILE} ({exc}).")


def _is_stale(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return True
    ttl = MAX_AGE_DAYS if entry.get("sector") else UNKNOWN_AGE_DAYS
    return _age_days(entry.get("asof")) >= ttl


def refresh_sectors(
    data_dir: str,
    tickers: list[str],
    max_lookups: int = MAX_LOOKUPS_PER_RUN,
) -> dict[str, Any]:
    """Bring data/sectors.json up to date for `tickers` (held stocks plus option
    underlyings). Looks up at most `max_lookups` missing/stale names per call,
    preserves `overrides`, and never raises."""
    store = load_sectors(data_dir)
    known: dict[str, Any] = store["tickers"]

    wanted = sorted({s.strip().upper() for s in tickers if is_lookup_worthy(s)})
    todo = [s for s in wanted if _is_stale(known.get(s))]
    if not todo:
        return store

    # Oldest first, so a long-stale entry isn't starved by new tickers forever.
    todo.sort(key=lambda s: -_age_days((known.get(s) or {}).get("asof")))
    batch = todo[:max_lookups]
    print(f"  sectors: looking up {len(batch)} of {len(todo)} missing/stale ticker(s) ...")

    changed = False
    for sym in batch:
        res = lookup_sector(sym)
        if res is None:
            continue  # transient failure: keep whatever was cached, retry next run
        res["asof"] = _now_iso()
        known[sym] = res
        changed = True
        label = res["sector"] or "unclassified"
        print(f"  sectors: {sym} -> {label}")

    if changed:
        store["asof"] = _now_iso()
        _save(data_dir, store)
    return store


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    d = os.environ.get("APP_DATA_DIR", ".")
    syms = sys.argv[1:] or ["AAPL", "SMH", "SWVXX"]
    print(f"Sector lookup test (data dir: {d})")
    out = refresh_sectors(d, syms, max_lookups=len(syms))
    for s in syms:
        print(f"  {s:<8} {out['tickers'].get(s.upper())}")
    print(f"Wrote data/{SECTORS_FILE}. Add {{\"overrides\": {{\"TICKER\": \"Sector\"}}}} there to correct any of these.")
