"""
probe_indices.py
================
One-off check: does Schwab's quote endpoint serve these CBOE index symbols for
YOUR account? This tells us which of the remaining VIX-panel indicators we can
wire straight from Schwab vs. need a yfinance (^VVIX / ^SKEW) fallback.

$VIX and $VIX3M are already used by the app/AM report, so they're here only as a
sanity check that quoting works at all. $VVIX, $SKEW, $VIX9D are the unknowns.

Read-only. Makes one quote call. Places no orders.

Run:
    python probe_indices.py
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import schwab_client as sc

# Known-good first (sanity), then the ones we actually want to learn about.
SYMBOLS = ["$VIX", "$VIX3M", "$VIX9D", "$VVIX", "$SKEW"]


def main() -> None:
    c = sc.get_client()
    print("Probing Schwab quotes for: " + ", ".join(SYMBOLS) + "\n")
    try:
        prices = sc.get_quotes(c, SYMBOLS)
    except Exception as exc:
        print(f"Quote call failed entirely: {exc}")
        print("(If this is a Market Data product error, that's an account-level entitlement, not a symbol issue.)")
        return

    for s in SYMBOLS:
        v = prices.get(s)
        print(f"  {'OK  ' if v is not None else 'MISS'}  {s:7s} = {v if v is not None else '— no quote —'}")

    got = [s for s in SYMBOLS if prices.get(s) is not None]
    miss = [s for s in SYMBOLS if prices.get(s) is None]
    print()
    print("Schwab serves:   " + (", ".join(got) if got else "none"))
    if miss:
        print("yfinance fallback for: " + ", ".join(m.replace("$", "^") for m in miss))
        print("(yfinance is already installed for fetch_earnings.py.)")
    print("\nReport these results back and I'll wire whatever's available.")


if __name__ == "__main__":
    main()
