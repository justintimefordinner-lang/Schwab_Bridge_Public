"""
probe_s5fi.py
=============
Does Schwab's market-data API serve $SPXA50R (S&P 500 % above their 50-day SMA)
for YOUR account? This breadth symbol is a StockCharts convention, so Schwab may
not carry it even though it shows in thinkorswim.

Checks BOTH endpoints the indicator needs — the quote (current level) and price
history (the weekly-slope source) — across a few symbol spellings, and prints the
raw shape of whatever comes back so we can see exactly what's available.

Read-only. Makes a handful of quote/history calls. Places no orders.

Run:
    python probe_s5fi.py
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import schwab_client as sc

# $SPXA50R is the one we want; the rest are spelling/format fallbacks plus $SPX
# as a known-good sanity check that index quoting works at all on this account.
QUOTE_VARIANTS = ["$SPXA50R", "SPXA50R", "$SPXA50R.X", "$SPXA200R", "$SPX"]
HISTORY_VARIANTS = ["$SPXA50R", "SPXA50R", "$SPXA50R.X"]


def main() -> None:
    c = sc.get_client()

    print("=== QUOTE endpoint ===")
    served = []
    for s in QUOTE_VARIANTS:
        try:
            resp = c.get_quotes([s])
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            print(f"  ERR   {s:12s} -> {exc}")
            continue
        payload = data.get(s)
        if not payload:
            print(f"  MISS  {s:12s} -> not in response (returned keys: {list(data.keys())})")
            continue
        q = (payload or {}).get("quote", {}) or {}
        last = q.get("lastPrice")
        if last is None:
            last = q.get("mark")
        if last is None:
            last = q.get("closePrice")
        flag = "OK  " if last is not None else "MISS"
        print(f"  {flag}  {s:12s} -> last={last}  sample fields={sorted(q.keys())[:8]}")
        if last is not None:
            served.append(s)

    print("\n=== PRICE HISTORY endpoint (daily candles, ~120d) ===")
    hist_ok = []
    for s in HISTORY_VARIANTS:
        candles = sc.get_price_history(c, s, days=120)
        if candles:
            last_close = candles[-1].get("close")
            print(f"  OK    {s:12s} -> {len(candles)} candles, last close={last_close}")
            hist_ok.append(s)
        else:
            print(f"  MISS  {s:12s} -> 0 candles")

    print("\n--- verdict ---")
    if served or hist_ok:
        good = served[0] if served else hist_ok[0]
        print(f"Schwab serves S5FI as '{good}'. If the app still shows n/a, the quote key")
        print("written to vix.json must match that exact spelling.")
    else:
        print("Schwab's API does NOT carry $SPXA50R for this account (expected for a")
        print("StockCharts breadth symbol). Report this output and I'll wire a fallback:")
        print("  • compute S5FI from the S&P 500 constituents (50-day SMA, % above), or")
        print("  • a different breadth source.")


if __name__ == "__main__":
    main()
