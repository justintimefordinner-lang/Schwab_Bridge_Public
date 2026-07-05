"""Build research.json for the app's Research screen + action center.

For each approved ticker: pull ~1y of daily candles, fold the live quote in as
today's close, compute Bollinger %B / RSI / MACD, and classify bullish/bearish
setups. Read-only market data; writes a single JSON the app reads per request.

Run standalone (`python research_sync.py`) or via auto_push on a slow cadence —
daily indicators only move once per close, so intraday we just re-fold the live
quote.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import indicators

RESEARCH_FILE = "research.json"

# Mirror of lib/approved-stocks.ts, used only if that file can't be read.
_FALLBACK_APPROVED = [
    "AMD", "VRT", "PLTR", "FUTU", "SHOP", "DELL", "CRDO", "ANET", "HOOD", "WDC",
    "CCJ", "KTOS", "FTNT", "INOD", "CSCO", "IBIT", "META", "APP", "MSFT", "TSLA",
    "AXP", "AVGO", "GE", "JPM", "CLS", "TSM", "AAPL", "GOOGL", "STX", "AMZN",
    "MU", "NVDA", "ETHA", "SOFI", "CDE", "IREN", "AA", "ADI", "CCL", "HL",
    "AMAT", "LRCX", "APH", "EQT", "NEM", "CAT", "FCX", "RTX", "GLW", "COHR",
    "DRAM", "INTC", "SMH", "CEG", "NBIS", "TER",
]


def _app_data_dir() -> str:
    d = os.environ.get("APP_DATA_DIR")
    if not d:
        raise SystemExit(
            "Set APP_DATA_DIR in your .env to the app's data folder, e.g.\n"
            "  APP_DATA_DIR=C:\\Claude_Codes\\Stock_Portfolio_Tracker\\data"
        )
    if not os.path.isdir(d):
        raise SystemExit(f"APP_DATA_DIR '{d}' does not exist.")
    return d


def load_approved(data_dir: str) -> list[str]:
    """Approved roster. Prefer the app's editable store (data/approved-stocks.json,
    written when you add/remove names in the app); fall back to parsing the seed
    lib/approved-stocks.ts, then a baked-in copy. One source of truth, app-editable."""
    jpath = os.path.join(data_dir, "approved-stocks.json")
    try:
        with open(jpath, encoding="utf-8") as f:
            store = json.load(f)
        syms = store.get("symbols")
        if isinstance(syms, list) and syms:
            return [str(s).strip().upper() for s in syms if str(s).strip()]
    except (OSError, json.JSONDecodeError):
        pass

    app_root = os.path.dirname(os.path.abspath(data_dir))
    ts_path = os.path.join(app_root, "lib", "approved-stocks.ts")
    try:
        text = open(ts_path, encoding="utf-8").read()
        m = re.search(r"APPROVED_STOCKS:\s*readonly string\[\]\s*=\s*\[(.*?)\];", text, re.S)
        if m:
            syms = re.findall(r'"([A-Z0-9.]+)"', m.group(1))
            if syms:
                return syms
    except OSError:
        pass
    return list(_FALLBACK_APPROVED)


def load_held_symbols(data_dir: str) -> list[str]:
    """Equity tickers currently held (any account) from snapshot.json, so covered
    calls can be evaluated on real positions even if a name isn't in the approved
    roster. Empty if the snapshot isn't there yet."""
    try:
        with open(os.path.join(data_dir, "snapshot.json"), encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    syms: set[str] = set()
    for acc in (snap.get("data") or {}).values():
        for e in acc.get("equities", []) or []:
            s = e.get("symbol")
            if s:
                syms.add(s)
    return sorted(syms)


def _today_eastern():
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date(), ZoneInfo("America/New_York")
    except Exception:
        return datetime.now(timezone.utc).date(), timezone.utc


def closes_with_live(candles: list[dict[str, Any]], live_price: float | None) -> list[float]:
    """Closing prices oldest→newest, with the live quote folded in as today's
    close (dropping any same-day partial candle so we don't double-count)."""
    closes = [float(cd["close"]) for cd in candles if cd.get("close") is not None]
    today, tz = _today_eastern()
    last_ts = candles[-1].get("datetime") if candles else None
    if last_ts and closes:
        try:
            last_d = datetime.fromtimestamp(last_ts / 1000, tz).date()
            if last_d == today:
                closes = closes[:-1]
        except (OverflowError, OSError, ValueError):
            pass
    if live_price is not None:
        closes.append(float(live_price))
    return closes


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    import schwab_client as sc

    data_dir = _app_data_dir()
    approved = load_approved(data_dir)
    held = load_held_symbols(data_dir)
    # Approved universe + any held names not on it (so covered calls have data).
    extra = [s for s in held if s not in approved]
    symbols = approved + extra
    print(f"Research sync: {len(approved)} approved + {len(extra)} held-only = {len(symbols)} names")

    c = sc.get_client()
    live: dict[str, float] = {}
    try:
        live = sc.get_quotes(c, symbols)
    except Exception as exc:  # market-data product missing, etc.
        print(f"  note: live quotes unavailable ({exc}); using last candle close.")

    tickers: dict[str, Any] = {}
    for sym in symbols:
        candles = sc.get_price_history(c, sym)
        if not candles:
            tickers[sym] = {"error": "no history"}
            continue
        closes = closes_with_live(candles, live.get(sym))
        ind = indicators.compute_indicators(closes)
        if ind is None:
            tickers[sym] = {"error": "insufficient history"}
            continue
        tickers[sym] = {**ind, "setup": indicators.classify(ind)}

    # Flatten the fired setups into a list the action center can read directly.
    signals: list[dict[str, Any]] = []
    for sym, t in tickers.items():
        sig = (t.get("setup") or {}).get("signal") if isinstance(t, dict) else None
        if sig:
            signals.append({
                "symbol": sym,
                "direction": sig["direction"],
                "strength": sig["strength"],
                "score": sig.get("score"),
                "vehicles": sig["vehicles"],
                "price": t.get("price"),
                "pctB": t.get("pctB"),
                "rsi": t.get("rsi"),
                "hist": t.get("hist"),
            })
    rank = {"strong": 0, "forming": 1}
    signals.sort(key=lambda s: (rank.get(s["strength"], 9), -(s.get("score") or 0), s["symbol"]))

    out = {
        "meta": {
            "asOf": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "count": len(symbols),
            "params": indicators.PARAMS,
        },
        "tickers": tickers,
        "signals": signals,
    }
    path = os.path.join(data_dir, RESEARCH_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    strong = sum(1 for s in signals if s["strength"] == "strong")
    print(f"  wrote {len(symbols)} names, {len(signals)} signals ({strong} strong) -> {path}")


if __name__ == "__main__":
    main()
