"""
s5fi_breadth.py
===============
S5FI = the share of S&P 500 stocks trading above their own 50-day SMA (StockCharts
$SPXA50R). Schwab's market-data API does NOT serve $SPXA50R, so we compute the
indicator from its definition using yfinance (already a dependency of
fetch_earnings.py):

  1. get the current S&P 500 constituents (Wikipedia, cached ~weekly),
  2. batch-download ~8 months of daily closes for all of them,
  3. for every date, count the % of names whose close is above their 50-day SMA.

Computing it across the whole history (not just today) yields a real S5FI *series*,
so the weekly slope and the app's sparkline work immediately.

Because a 500-ticker download is heavy, get_s5fi() caches the result in
data/s5fi_cache.json and only recomputes when the cache is older than `max_age_hours`
(default 12h). Every function degrades to None / stale cache rather than raising, so
a bad network day can't break the export.

Standalone test (prints level / slope / recent weekly series):
    python s5fi_breadth.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

CACHE_FILE = "s5fi_cache.json"
TICKERS_FILE = "sp500_tickers.json"
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Tunables. The 50-day SMA is the breadth threshold each name is measured against,
# so it sets the *floor* on how much history must be pulled (you can't compute the
# indicator at all with less). SLOPE/SPARK are weekly-close counts; the pull is
# sized to SMA_DAYS + SPARK_WEEKS*5 + buffer, so shrinking SPARK_WEEKS — not the
# slope — is what actually trims the download.
SMA_DAYS = 50        # 50-day SMA threshold
SLOPE_WEEKS = 6      # weekly closes used for the trend slope (recent-weighted)
SPARK_WEEKS = 8      # weekly closes shown in the app sparkline


# --------------------------------------------------------------------------- universe
def _fetch_sp500_from_web() -> list[str]:
    """Current S&P 500 tickers (yfinance form: BRK.B -> BRK-B) from the web. Tries
    Wikipedia first WITH a browser User-Agent — Wikipedia 403s urllib's default
    agent, which is the usual reason a bare pd.read_html(url) silently returns
    nothing — then falls back to a stable CSV mirror."""
    import io
    import requests
    import pandas as pd
    headers = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-tracker/1.0)"}

    try:
        resp = requests.get(WIKI_URL, headers=headers, timeout=20)
        resp.raise_for_status()
        for t in pd.read_html(io.StringIO(resp.text)):
            if "Symbol" in t.columns:
                syms = t["Symbol"].astype(str).str.strip()
                tickers = sorted({s.replace(".", "-") for s in syms if s and s.lower() != "nan"})
                if len(tickers) >= 400:
                    return tickers
    except Exception as exc:
        print(f"  note: Wikipedia S&P 500 fetch failed ({exc}).")

    # CSV mirrors need no HTML parser (pd.read_csv), so these work even without lxml.
    csv_urls = [
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
    ]
    for url in csv_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            tickers = sorted({str(s).strip().replace(".", "-") for s in df[col]
                              if str(s).strip() and str(s).strip().lower() != "nan"})
            if len(tickers) >= 400:
                return tickers
        except Exception as exc:
            print(f"  note: CSV mirror ({url.rsplit('/', 3)[1]}) failed ({exc}).")

    return []


def _load_sp500_tickers(data_dir: str, max_age_days: int = 7) -> list[str]:
    """Current S&P 500 tickers, cached ~weekly in data/sp500_tickers.json. Returns []
    only if both the cache and every web source fail."""
    path = os.path.join(data_dir, TICKERS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        asof = datetime.fromisoformat(cached["asof"])
        if (datetime.now(timezone.utc) - asof).total_seconds() / 86400 < max_age_days and cached.get("tickers"):
            return cached["tickers"]
    except Exception:
        pass
    tickers = _fetch_sp500_from_web()
    if len(tickers) >= 400:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"asof": datetime.now(timezone.utc).isoformat(), "tickers": tickers}, f)
        except Exception:
            pass
        return tickers
    try:  # stale cache beats nothing
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("tickers", [])
    except Exception:
        return []


# --------------------------------------------------------------------------- compute
def _slope(series: list[float]) -> float | None:
    n = len(series)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(series) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    num = sum((x - mx) * (y - my) for x, y in zip(xs, series))
    return round(num / den, 3)


def compute_s5fi(tickers: list[str]):
    """Return (level, slopeWk, weekly_list) computed from constituents, or
    (None, None, None) on failure. level = today's % above 50-day SMA; weekly_list =
    last ~12 weekly (Fri) closes of the S5FI series; slopeWk = least-squares slope of
    the last 8 of those (pts/week)."""
    if not tickers:
        print("  note: S5FI has no constituent list (web fetch + cache both empty).")
        return None, None, None
    print(f"  S5FI: computing from {len(tickers)} constituents ...")
    try:
        import yfinance as yf
        # Pull only as far back as needed: 50-day SMA warmup + the weekly span we
        # display + a small buffer. yfinance `period=` accepts only fixed strings
        # (6mo/1y/...), so size it with an explicit start date instead.
        td_needed = SMA_DAYS + SPARK_WEEKS * 5 + 15
        start = datetime.now() - timedelta(days=int(td_needed * 7 / 5) + 7)
        data = yf.download(tickers, start=start.strftime("%Y-%m-%d"), interval="1d",
                           auto_adjust=True, progress=False, threads=True)
    except Exception as exc:
        print(f"  note: S5FI price download failed ({exc}).")
        return None, None, None
    try:
        import pandas as pd
        close = data["Close"] if isinstance(data.columns, pd.MultiIndex) else data
        if close is None or close.empty:
            print("  note: S5FI price download returned no rows (Yahoo may be rate-limiting).")
            return None, None, None
        sma = close.rolling(SMA_DAYS, min_periods=SMA_DAYS).mean()
        valid = sma.notna() & close.notna()
        above = (close > sma) & valid
        denom = valid.sum(axis=1)
        pct = (above.sum(axis=1) / denom.where(denom > 0)) * 100.0
        pct = pct.dropna()
        print(f"  S5FI: frame {close.shape[0]}d x {close.shape[1]} tickers; {len(pct)} valid days")
        if pct.empty:
            return None, None, None
        level = round(float(pct.iloc[-1]), 1)
        weekly_ser = pct.resample("W-FRI").last().dropna()
        weekly = [round(float(v), 1) for v in weekly_ser.tail(SPARK_WEEKS)]
        slope = _slope(weekly[-SLOPE_WEEKS:]) if len(weekly) >= 3 else None
        return level, slope, (weekly or None)
    except Exception as exc:
        print(f"  note: S5FI compute failed ({exc}).")
        return None, None, None


# --------------------------------------------------------------------------- cached entry
def get_s5fi(data_dir: str, max_age_hours: float = 12.0) -> dict | None:
    """{'level','slopeWk','weekly','asof'} for the app, cached in data/s5fi_cache.json.
    Recomputes only when the cache is older than `max_age_hours`. On compute failure,
    returns the stale cache if present, else None."""
    path = os.path.join(data_dir, CACHE_FILE)
    cached = None
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        asof = datetime.fromisoformat(cached["asof"])
        age_h = (datetime.now(timezone.utc) - asof).total_seconds() / 3600
        if age_h < max_age_hours and cached.get("level") is not None:
            return cached
    except Exception:
        pass

    tickers = _load_sp500_tickers(data_dir)
    level, slope, weekly = compute_s5fi(tickers)
    if level is None:
        return cached  # stale cache or None
    result = {
        "level": level,
        "slopeWk": slope,
        "weekly": weekly,
        "asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n": len(tickers),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    d = os.environ.get("APP_DATA_DIR", ".")
    print(f"S5FI standalone test (data dir: {d})")
    tks = _load_sp500_tickers(d, max_age_days=0)  # force a fresh constituent fetch
    print(f"  constituents fetched: {len(tks)}  e.g. {tks[:5]}")
    if len(tks) < 400:
        print("  -> constituent list failed; that's the blocker. (Wikipedia 403 / network?)")
    out = get_s5fi(d, max_age_hours=0)  # force a fresh compute
    if not out:
        print("No result — the notes above show the failing stage.")
    else:
        print(f"  level   : {out['level']}  ({out.get('n')} constituents)")
        print(f"  slopeWk : {out['slopeWk']}  (pts/week, last {SLOPE_WEEKS} weekly closes)")
        print(f"  weekly  : {out['weekly']}")
        print(f"  asof    : {out['asof']}")
        print("Wrote data/s5fi_cache.json — restart auto_push.py to pick it up.")
