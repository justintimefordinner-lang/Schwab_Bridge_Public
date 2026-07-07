"""
export_to_app.py
================

The BRIDGE between your read-only Schwab data layer (schwab_client.py) and
your brother's Next.js portfolio app.

It reuses the SAME authenticated connection and the SAME parsing the
Streamlit dashboard uses (schwab_client.get_account_snapshot), then maps
the result into the exact `Snapshot` shape the app reads from
`data/snapshot.json`. Your Schwab keys never leave Python; the app only
ever sees clean JSON.

This file does NOT modify schwab_client.py. The only extra Schwab call it
makes is one option-quote batch per account to pull the greeks the app
displays (delta / IV / mark) that the dashboard didn't need.

Run with:
    python export_to_app.py

One-time setup: add this line to your .env, pointing at the app's data
folder (the folder that contains snapshot.json):
    APP_DATA_DIR=C:\\Claude_Codes\\Stock_Portfolio_Tracker\\data

Read-only: this never places or cancels an order.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SNAPSHOT_FILE = "snapshot.json"
HISTORY_FILE = "value-history.json"  # our own rolling equity-curve store
VIX_FILE = "vix.json"                # live VIX level for the app's VIX tab
HISTORY_MAX = 365                    # keep at most ~1y of daily points
SOURCE_LABEL = "schwab-bridge"


def _realized_vol_pct(closes: list[float], n: int = 20) -> float | None:
    """Annualized close-to-close realized vol over the last n sessions, as a PERCENT
    (e.g. 12.5 → 12.5%). Same math as am_report.realized_vol (sample stdev of log
    returns × √252), ×100 so it's directly comparable to the VIX in points — the app
    computes VRP = VIX − RV. Needs n+1 valid closes; returns None otherwise."""
    closes = [float(x) for x in closes if x is not None and float(x) > 0]
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def _weekly_series(candles: list[dict], weeks: int = 12) -> list[float]:
    """Weekly closes (oldest→newest): one per ISO week = that week's last daily
    close, limited to the most recent `weeks`. Drives the app's S5FI sparkline."""
    from datetime import datetime as _dt
    by_week: dict[tuple[int, int], float] = {}
    for k in candles:
        ts, close = k.get("datetime"), k.get("close")
        if ts is None or close is None:
            continue
        iso = _dt.utcfromtimestamp(ts / 1000.0).isocalendar()
        by_week[(iso[0], iso[1])] = round(float(close), 2)  # later candle = week's last close
    return [by_week[w] for w in sorted(by_week)][-weeks:]


def _slope(series: list[float]) -> float | None:
    """Least-squares slope (units per step) of an evenly-spaced series. None when
    fewer than 3 points or degenerate."""
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


def _weekly_slope(candles: list[dict], weeks: int = 8) -> float | None:
    """Slope of weekly closes (level-points per week) over the last `weeks`."""
    return _slope(_weekly_series(candles, weeks))


def _app_data_dir() -> str:
    d = os.environ.get("APP_DATA_DIR")
    if not d:
        raise SystemExit(
            "Set APP_DATA_DIR in your .env to the app's data folder, e.g.\n"
            "  APP_DATA_DIR=C:\\Claude_Codes\\Stock_Portfolio_Tracker\\data"
        )
    if not os.path.isdir(d):
        raise SystemExit(
            f"APP_DATA_DIR '{d}' does not exist. Point it at the folder that "
            "contains the app's snapshot.json."
        )
    return d


# ---------------------------------------------------------------------------
# Category (Schwab classifier) -> app OptionKind
# ---------------------------------------------------------------------------
# The app originally knew only csp / leap-call / leap-put-hedge. We extend it
# (see types.ts patch) to also carry covered-call / put-spread / call-spread /
# other, so nothing your classifier produces is silently dropped.
def _kind_for(p: dict[str, Any]) -> str:
    cat = p.get("category")
    if cat == "CSPs":
        return "csp"
    if cat == "LEAPS":
        return "leap-call"
    if cat == "Covered calls":
        return "covered-call"
    if cat == "Put spreads":
        return "put-spread"
    if cat == "Call spreads":
        return "call-spread"
    # "Other": a long-dated LONG put is a protective hedge in the app's model.
    pc = (p.get("put_call") or "").upper()
    is_put = pc in ("PUT", "P")
    qty = p.get("quantity") or 0  # signed: + long, - short
    dte = p.get("dte")
    if is_put and qty > 0 and (dte is None or dte >= 365):
        return "leap-put-hedge"
    return "other"


# ---------------------------------------------------------------------------
# Option greeks (the one extra Schwab call). Defensive about field names the
# same way schwab_client is, since Schwab's quote payload varies a little.
# ---------------------------------------------------------------------------
def _to_iv_decimal(vol: float | None) -> float | None:
    """Schwab reports IV as a percent (e.g. 42.3). Normalize to a decimal."""
    if vol is None:
        return None
    return vol / 100.0 if vol > 5 else vol


BB_CACHE_FILE = "bb_bands.json"     # per-underlying 20-day Bollinger bands, cached daily


def _bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    """20-day Bollinger bands: SMA(n) ± k·σ(n) (population σ, StockCharts convention).
    None until n closes exist or on a degenerate (flat) window."""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mean = sum(window) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in window) / n)
    if sd <= 0:
        return None
    return {"mid": round(mean, 2), "sd": round(sd, 4),
            "upper": round(mean + k * sd, 2), "lower": round(mean - k * sd, 2)}


def _strike_sigma(strike: float | None, bands: dict | None) -> float | None:
    """A strike's distance from the 20-day mean in σ (−2 = lower band, +2 = upper)."""
    if not strike or not bands or not bands.get("sd"):
        return None
    return round((strike - bands["mid"]) / bands["sd"], 2)


def _bands_for(sc, c, ticker: str, cache: dict, today_str: str) -> dict | None:
    """20-day bands for one underlying, cached per calendar day so we pull daily
    history at most once per ticker per day (not on every 60s app push). A failed or
    thin fetch does NOT overwrite a good cached value: we keep showing the last bands we
    had (20-day bands barely move day to day) rather than blanking the column, and we
    never cache a None — so a transient price-history hiccup simply retries next push."""
    ent = cache.get(ticker) or {}
    if ent.get("asof") == today_str and ent.get("bands"):
        return ent["bands"]
    bands = None
    try:
        closes = [k["close"] for k in sc.get_price_history(c, ticker, days=60)
                  if k.get("close") is not None]
        bands = _bollinger(closes)
    except Exception as exc:
        print(f"  note: BB bands unavailable for {ticker} ({exc}).")
    if bands:
        cache[ticker] = {"asof": today_str, "bands": bands}
        return bands
    # Fetch failed or returned <20 closes — fall back to the last good bands (if any)
    # instead of dropping bbSigma to null, which renders as an empty "—".
    return ent.get("bands")


def _enrich_bb(sc, c, account_data: dict, cache: dict, today_str: str) -> None:
    """Tag every option leg AND every held stock with bbSigma — the strike (or the
    stock's own price) vs the underlying's 20-day Bollinger bands. For CSPs / covered
    calls the leg is the short strike; for spread legs the app picks the short leg's
    value. Bands fetched once per unique underlying."""
    opts = account_data.get("options", [])
    eqs = account_data.get("equities", [])
    syms = {o.get("symbol") for o in opts if o.get("symbol") and o.get("strike")}
    syms |= {e.get("symbol") for e in eqs if e.get("symbol") and e.get("price")}
    bands_by_sym = {s: _bands_for(sc, c, s, cache, today_str) for s in syms}
    for o in opts:
        sig = _strike_sigma(o.get("strike"), bands_by_sym.get(o.get("symbol")))
        if sig is not None:
            o["bbSigma"] = sig
    for e in eqs:
        sig = _strike_sigma(e.get("price"), bands_by_sym.get(e.get("symbol")))
        if sig is not None:
            e["bbSigma"] = sig


def get_option_greeks(c, option_symbols: list[str]) -> dict[str, dict[str, float]]:
    """Return {option_symbol: {delta, iv, mark, theta}} from market data.

    Requires the 'Market Data Production' product (same as the dashboard's
    quotes). On failure returns {} and the caller falls back to per-position
    values, so a missing greek never crashes the export.
    """
    wanted = sorted({s for s in option_symbols if s})
    if not wanted:
        return {}
    resp = c.get_quotes(wanted)
    resp.raise_for_status()
    data = resp.json() or {}
    out: dict[str, dict[str, float]] = {}
    for sym, payload in data.items():
        q = (payload or {}).get("quote", {}) or {}
        mark = q.get("mark")
        if mark is None:
            mark = q.get("markPrice") or q.get("lastPrice") or q.get("closePrice")
        out[sym] = {
            "delta": q.get("delta"),
            "gamma": q.get("gamma"),  # dΔ/dS — for the after-hours Simulate projection
            "vega": q.get("vega"),    # dV/dσ — for the (assumed) IV-shift term in Simulate
            "iv": _to_iv_decimal(q.get("volatility")),
            "mark": mark,
            "theta": q.get("theta"),
            "netChange": q.get("netChange"),  # per-share $ change today (for Top Movers)
            # The underlying price THIS frozen mark/greeks were computed against — the
            # true ΔS=0 reference for the Simulate projection (never yesterday's close).
            "underClose": q.get("underlyingPrice"),
        }
    return out


# ---------------------------------------------------------------------------
# Pure mapping (no Schwab calls here, so it's unit-testable offline)
# ---------------------------------------------------------------------------
def _round(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if isinstance(v, (int, float)) else None


def fetch_stock_day(c, tickers: list[str]) -> dict[str, dict[str, float | None]]:
    """Per-ticker current price + day point-change (vs prior close) from equity
    quotes. Powers the Top Movers tiles (underlying line + equity day-value change)
    for every held stock AND every option underlying. Returns {ticker: {price,
    change}}; empty on failure so the export degrades to no movers rather than crash."""
    wanted = sorted({t for t in tickers if t})
    if not wanted:
        return {}
    try:
        resp = c.get_quotes(wanted)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        print(f"  note: stock day-quotes unavailable ({exc}).")
        return {}
    out: dict[str, dict[str, float | None]] = {}
    for sym, payload in data.items():
        q = (payload or {}).get("quote", {}) or {}
        last = q.get("lastPrice")
        if last is None:
            last = q.get("mark")
        # Regular-session close = the price the frozen option marks correspond to;
        # `last` keeps moving after hours. Their gap is the ΔS the Simulate toggle uses.
        reg_close = q.get("regularMarketLastPrice")
        if reg_close is None:
            reg_close = q.get("closePrice")
        price = last if last is not None else reg_close
        change = q.get("netChange")
        if change is None and price is not None and q.get("closePrice"):
            change = price - q["closePrice"]
        out[sym] = {"price": price, "change": change, "close": reg_close, "live": last}
    return out


def map_equity(p: dict[str, Any], stock_day: dict | None = None) -> dict[str, Any]:
    ticker = p.get("ticker", "")
    qty = p.get("quantity") or 0
    price = p.get("underlying_price")
    if price is None and qty:
        mv = p.get("market_value") or 0
        price = mv / qty if qty else None
    sd = (stock_day or {}).get(ticker, {})
    return {
        "symbol": ticker,
        "name": ticker,  # show the ticker, not the full company name
        "qty": qty,
        "avgCost": _round(p.get("avg_price")),
        "price": _round(price),
        "dayChange": _round(sd.get("change")),  # per-share $ move today (Top Movers)
    }


def map_option(p: dict[str, Any], greeks: dict[str, dict[str, float]], open_dates: dict[str, str] | None = None, stock_day: dict | None = None) -> dict[str, Any]:
    g = greeks.get(p.get("symbol", ""), {})
    pc = (p.get("put_call") or "").upper()
    is_put = pc in ("PUT", "P")
    signed_qty = p.get("quantity") or 0
    qty = abs(signed_qty)
    side = "short" if signed_qty < 0 else "long"
    entry = abs(p.get("avg_price") or 0.0)          # per share, positive
    strike = p.get("strike") or 0.0

    mark = g.get("mark")
    if mark is None:
        mv = abs(p.get("market_value") or 0.0)
        mark = mv / (100 * qty) if qty else 0.0

    delta = g.get("delta")
    theta = g.get("theta")
    if theta is None:
        theta = p.get("theta")  # per-share theta from the dashboard's pull

    breakeven = (strike + entry) if not is_put else (strike - entry)

    # Day's change in THIS leg's market value: long gains when the option rises,
    # a short gains when it falls — so sign by side. netChange is per-share.
    net_ch = g.get("netChange")
    day_val = None
    if net_ch is not None and qty:
        side_sign = -1.0 if side == "short" else 1.0
        day_val = side_sign * net_ch * 100 * qty
    # Simulate reference = the underlying the frozen mark/greeks were priced at. Prefer
    # the option quote's own underlyingPrice; fall back to the position's underlying_price.
    # (Never the equity closePrice — that's the PRIOR day and inflates ΔS across sessions.)
    under_close = g.get("underClose")
    if under_close is None:
        under_close = p.get("underlying_price")
    under_live = (stock_day or {}).get(p.get("ticker", ""), {}).get("live")
    if under_live is None:
        under_live = (stock_day or {}).get(p.get("ticker", ""), {}).get("price")
    sd = (stock_day or {}).get(p.get("ticker", ""), {})

    opt: dict[str, Any] = {
        "id": p.get("symbol", ""),
        "kind": _kind_for(p),
        "symbol": p.get("ticker", ""),
        "optionType": "put" if is_put else "call",
        "side": side,
        "qty": qty,
        "strike": _round(strike),
        "expiration": p.get("expiration"),
        "entryPerShare": _round(entry),
        "mark": _round(mark),
        "delta": _round(delta) if delta is not None else 0.0,
        "gamma": _round(g.get("gamma"), 4) if g.get("gamma") is not None else 0.0,
        "vega": _round(g.get("vega"), 4) if g.get("vega") is not None else 0.0,
        "theta": _round(theta) if theta is not None else 0.0,
        "iv": _round(g.get("iv"), 4) if g.get("iv") is not None else 0.0,
        "breakeven": _round(breakeven),
        "underlyingPrice": _round(p.get("underlying_price")),
        "underlyingChange": _round(sd.get("change")),  # underlying per-share $ move today
        "underlyingClose": _round(under_close),  # regular-close ref (option's own underlying)
        "underlyingLive": _round(under_live),  # current/after-hours last (Simulate target)
        "dayValueChange": _round(day_val),  # this leg's signed $ value move today
    }
    if side == "short" and delta is not None:
        opt["chanceOfProfitShort"] = _round(max(0.0, min(1.0, 1 - abs(delta))), 3)
    opened = (open_dates or {}).get(p.get("symbol", ""))
    if opened:
        opt["openedAt"] = opened  # derived from order history; omitted if unknown
    return opt


def build_account_data(
    account_snapshot: dict[str, Any],
    greeks: dict[str, dict[str, float]],
    value_history: list[dict[str, Any]],
    open_dates: dict[str, str] | None = None,
    stock_day: dict | None = None,
) -> dict[str, Any]:
    positions = account_snapshot.get("positions", [])
    equities = [map_equity(p, stock_day) for p in positions if p.get("category") == "Stock"]
    options = [
        map_option(p, greeks, open_dates, stock_day)
        for p in positions
        if (p.get("asset_type") or "").upper() == "OPTION"
    ]

    total = account_snapshot.get("liquidation_value") or 0.0
    cash = account_snapshot.get("cash") or 0.0
    crypto_value = 0.0  # Schwab has no crypto
    equity_value = sum((e["qty"] or 0) * (e["price"] or 0) for e in equities)
    # Reconciling plug so the app's four buckets sum to total, exactly as the
    # old seed did. Net options + any margin effects land here.
    options_value = total - equity_value - cash - crypto_value

    return {
        "summary": {
            "totalValue": _round(total),
            "equityValue": _round(equity_value),
            "optionsValue": _round(options_value),
            "cryptoValue": crypto_value,
            "cash": _round(cash),
            "buyingPower": _round(account_snapshot.get("buying_power") or 0.0),
            "optionsBuyingPower": _round(account_snapshot.get("options_bp") or 0.0),
        },
        "equities": equities,
        "options": options,
        "valueHistory": value_history,
    }


def build_snapshot(
    app_accounts: list[dict[str, Any]],
    data_by_account: dict[str, dict[str, Any]],
    prices_as_of: str,
) -> dict[str, Any]:
    return {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "pricesAsOf": prices_as_of,
            "source": SOURCE_LABEL,
        },
        "accounts": app_accounts,
        "data": data_by_account,
    }


# ---------------------------------------------------------------------------
# Rolling value history (the equity curve grows one point per day per account)
# ---------------------------------------------------------------------------
def load_history(data_dir: str) -> dict[str, list[dict[str, Any]]]:
    path = os.path.join(data_dir, HISTORY_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(data_dir: str, history: dict[str, list[dict[str, Any]]]) -> None:
    path = os.path.join(data_dir, HISTORY_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def update_history(
    history: dict[str, list[dict[str, Any]]],
    account_id: str,
    value: float | None,
    label: str,
) -> list[dict[str, Any]]:
    """Append today's value, or overwrite it if we already logged today.

    Returns the (capped) point list for this account.
    """
    points = history.setdefault(account_id, [])
    rounded = _round(value)
    if points and points[-1].get("label") == label:
        points[-1]["value"] = rounded
    else:
        points.append({"label": label, "value": rounded})
    if len(points) > HISTORY_MAX:
        del points[: len(points) - HISTORY_MAX]
    history[account_id] = points
    return points


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _load_order_store(data_dir: str) -> dict[str, list[dict[str, Any]]]:
    """The orders feed (trade-history.json) maintained by sync_trade_history,
    used to derive each open option's open date. Empty if absent."""
    try:
        with open(os.path.join(data_dir, "trade-history.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- covered calls
# Covered-call premiums for held stock (>=100 shares): the ~30-delta call at the
# expirations nearest 14 / 21 / 30 DTE, with premium % and annualized yield. Cached
# per ticker with a short TTL and refreshed only in market hours, so the 60s app push
# doesn't hammer Schwab option chains for every holding.
COVERED_CALL_CACHE_FILE = "covered_calls.json"
CC_TARGET_DTES = (14, 21, 30)
CC_TARGET_DELTA = 0.30
CC_TTL_SEC = 300  # re-pull a ticker's chain at most every ~5 minutes


def _cc_market_open() -> bool:
    """Rough US equity market-hours gate (weekday, 9:30-16:00 ET). Ignores holidays —
    on one it just serves cached premiums a little longer. Assumes open if the timezone
    can't be resolved, so we still refresh rather than go stale forever."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return True
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 570 <= mins < 960  # 9:30 AM .. 4:00 PM ET


def _call_delta(row: dict) -> float | None:
    """A call's delta from Schwab's per-contract value; None when blank (-999)."""
    dl = row.get("delta")
    if dl is None:
        return None
    try:
        dl = float(dl)
    except (TypeError, ValueError):
        return None
    return dl if 0.0 < dl <= 1.0 else None


def _covered_calls_from_chain(chain: dict, spot: float | None) -> list[dict] | None:
    """The ~30-delta call at the expirations nearest each target DTE, with premium
    per share, premium % of spot, and annualized yield. Distinct expirations."""
    if not chain or not spot:
        return None
    cmap = chain.get("callExpDateMap") or {}
    exps: list[tuple[int, str]] = []
    for key in cmap:
        try:
            exps.append((int(key.split(":")[1]), key))
        except (IndexError, ValueError):
            continue
    if not exps:
        return None
    out: list[dict] = []
    used: set[str] = set()
    for target in CC_TARGET_DTES:
        pool = [e for e in exps if e[1] not in used] or exps
        dte, key = min(pool, key=lambda e: abs(e[0] - target))
        used.add(key)
        best = None
        for strike_s, lst in cmap[key].items():
            row = lst[0]
            dl = _call_delta(row)
            if dl is None:
                continue
            diff = abs(dl - CC_TARGET_DELTA)
            if best is None or diff < best[0]:
                best = (diff, float(strike_s), dl, row)
        if best is None:
            continue
        _, strike, dl, row = best
        mark = row.get("mark")
        if mark is None:
            mark = ((row.get("bid") or 0.0) + (row.get("ask") or 0.0)) / 2
        prem_pct = (mark / spot * 100) if spot else 0.0
        out.append({
            "targetDte": target,
            "dte": dte,
            "strike": _round(strike),
            "delta": _round(dl),
            "mark": _round(mark),
            "premPct": _round(prem_pct),
            "annPct": round(prem_pct * 365 / dte, 1) if dte > 0 else None,
            "oi": int(row.get("openInterest") or 0),
        })
    return out or None


def _covered_calls_for(sc, c, ticker: str, spot: float | None, cache: dict,
                       now_ts: float, market_open: bool) -> dict:
    """Covered-call quotes AND gamma walls for a ticker, computed off ONE call+put
    chain fetch. Returns {"cc": [...]|None, "gamma": {...}|None}, cached with a short
    TTL and refreshed only in market hours."""
    ent = cache.get(ticker) or {}
    fresh = ent.get("ts") is not None and (now_ts - ent["ts"]) < CC_TTL_SEC
    if fresh or (not market_open and (ent.get("cc") is not None or ent.get("gamma") is not None)):
        return ent
    try:
        chain = sc.get_option_chain(c, ticker, days=40, strike_count=60, puts_only=False)
    except Exception:
        return ent
    if not chain:
        return ent
    from am_report import gamma_profile
    cc = _covered_calls_from_chain(chain, spot)
    gam = gamma_profile(chain, spot)
    if cc is None and gam is None:           # bad pull — keep whatever we had cached
        return ent
    upd = {
        "ts": now_ts,
        "cc": cc if cc is not None else ent.get("cc"),
        "gamma": gam if gam is not None else ent.get("gamma"),
    }
    cache[ticker] = upd
    return upd


def _enrich_covered_calls(sc, c, account_data: dict, cache: dict,
                          now_ts: float, market_open: bool) -> None:
    """Attach coveredCalls (~30-delta / 14/21/30 DTE) and gamma walls to each holding
    of >=100 shares, computed off a single call+put chain fetch per ticker."""
    for e in account_data.get("equities", []):
        if (e.get("qty") or 0) >= 100 and e.get("price"):
            res = _covered_calls_for(sc, c, e["symbol"], e.get("price"), cache, now_ts, market_open)
            if res.get("cc"):
                e["coveredCalls"] = res["cc"]
            if res.get("gamma"):
                e["gamma"] = res["gamma"]


def main() -> None:
    # Lazy import so the pure mapping above can be tested without schwab-py.
    from dotenv import load_dotenv

    load_dotenv()
    import schwab_client as sc

    data_dir = _app_data_dir()
    import closed_trades  # pure-Python; derives open dates from the orders feed
    order_store = _load_order_store(data_dir)
    c = sc.get_client()
    accounts = sc.list_accounts(c)
    if not accounts:
        raise SystemExit("No linked Schwab accounts found.")

    history = load_history(data_dir)
    today = date.today().isoformat()
    prices_as_of = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    app_accounts: list[dict[str, Any]] = []
    data_by_account: dict[str, dict[str, Any]] = {}
    try:
        with open(os.path.join(data_dir, BB_CACHE_FILE), encoding="utf-8") as f:
            bb_cache = json.load(f)
    except Exception:
        bb_cache = {}
    try:
        with open(os.path.join(data_dir, COVERED_CALL_CACHE_FILE), encoding="utf-8") as f:
            cc_cache = json.load(f)
    except Exception:
        cc_cache = {}
    cc_now = datetime.now().timestamp()
    cc_market_open = _cc_market_open()

    for i, acct in enumerate(accounts):
        acct_id = acct["hash"]                       # opaque, unique, app key
        last4 = (acct.get("number") or "")[-4:]
        print(f"Pulling Schwab account ****{last4} ...")

        snap = sc.get_account_snapshot(c, acct_id)
        if snap.get("quotes_error"):
            print(f"  note: {snap['quotes_error']}")

        opt_syms = [
            p["symbol"]
            for p in snap.get("positions", [])
            if (p.get("asset_type") or "").upper() == "OPTION"
        ]
        try:
            greeks = get_option_greeks(c, opt_syms) if opt_syms else {}
        except Exception as exc:  # market-data product missing, etc.
            print(f"  note: option greeks unavailable ({exc}); using fallbacks.")
            greeks = {}

        # Day price + point-change for every held stock AND every option underlying,
        # so the Top Movers tiles can aggregate the day's $ move per ticker.
        stock_syms = sorted({
            p.get("ticker") for p in snap.get("positions", [])
            if p.get("ticker") and (
                p.get("category") == "Stock" or (p.get("asset_type") or "").upper() == "OPTION"
            )
        })
        stock_day = fetch_stock_day(c, stock_syms) if stock_syms else {}

        points = update_history(history, acct_id, snap.get("liquidation_value"), today)

        open_dates = closed_trades.open_dates_by_occ(order_store.get(acct_id, []))

        app_accounts.append({
            "id": acct_id,
            "mask": f"\u2022\u2022\u2022\u2022{last4}",
            "type": (snap.get("account_type") or "margin").lower(),
            "brokerageType": "individual",
            "isDefault": i == 0,
        })
        data_by_account[acct_id] = build_account_data(snap, greeks, points, open_dates, stock_day)
        _enrich_bb(sc, c, data_by_account[acct_id], bb_cache, today)
        _enrich_covered_calls(sc, c, data_by_account[acct_id], cc_cache, cc_now, cc_market_open)

        # Opt-in sanity dump: set SIMULATE_DEBUG=1 to print, per leg, the full Simulate
        # chain — both underlying-close references (the option feed's underlyingPrice vs
        # the equity feed's regular close), the live price, ΔS, the frozen mark, the
        # greeks, and the resulting per-share ΔV + projected mark. A "REF MISMATCH" flag
        # fires when the two close references disagree (a common source of a spurious ΔS),
        # and "BIG ΔS" flags a move large enough that the 2nd-order (Δ/Γ) estimate — which
        # holds IV and time constant — will drift, especially on LEAPs (vega-heavy).
        if os.environ.get("SIMULATE_DEBUG"):
            for opt in data_by_account[acct_id]["options"]:
                uc, ul = opt.get("underlyingClose"), opt.get("underlyingLive")
                reg = (stock_day or {}).get(opt.get("symbol", ""), {}).get("close")
                ds = (ul - uc) if (uc is not None and ul is not None) else None
                mark = opt.get("mark")
                d, g = opt.get("delta") or 0.0, opt.get("gamma") or 0.0
                dv = (d * ds + 0.5 * g * ds * ds) if ds is not None else None
                proj = max(0.0, mark + dv) if (mark is not None and dv is not None) else None
                flags = ""
                if reg is not None and uc is not None and abs(reg - uc) > 0.01:
                    flags += "  <<< REF MISMATCH (opt-feed close != equity regular close)"
                if ds is not None and uc:
                    if abs(ds) / abs(uc) > 0.03:
                        flags += "  <<< BIG ΔS (>3%; Δ/Γ estimate unreliable)"
                print(f"  [sim] {opt.get('id',''):<21} uClose(opt)={uc} regClose(eq)={reg} "
                      f"live={ul} ΔS={_round(ds) if ds is not None else None} "
                      f"mark={mark} Δ={d} Γ={g} V={opt.get('vega')} ΔV/sh={_round(dv, 4) if dv is not None else None} "
                      f"projMark={_round(proj) if proj is not None else None}{flags}")

    try:
        with open(os.path.join(data_dir, BB_CACHE_FILE), "w", encoding="utf-8") as f:
            json.dump(bb_cache, f)
    except Exception:
        pass
    try:
        with open(os.path.join(data_dir, COVERED_CALL_CACHE_FILE), "w", encoding="utf-8") as f:
            json.dump(cc_cache, f)
    except Exception:
        pass

    snapshot = build_snapshot(app_accounts, data_by_account, prices_as_of)

    out_path = os.path.join(data_dir, SNAPSHOT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    save_history(data_dir, history)

    # Live VIX for the app's VIX tab. Only overwrite vix.json when we actually
    # got a level, so a transient market-data hiccup can't blank out the tab.
    try:
        vix_level = sc.get_vix(c)
    except Exception as exc:
        print(f"  note: VIX unavailable ({exc}).")
        vix_level = None
    if vix_level is not None:
        # Secondary CBOE indices for the indicator panel — all proven to quote on
        # this account (probe_indices.py: VIX3M/VIX9D/VVIX/SKEW all returned). One
        # batched quote call; each is independent, so a missing one stays None.
        vix3m = vix9d = vvix = skew = None
        try:
            idx = sc.get_quotes(c, ["$VIX3M", "$VIX9D", "$VVIX", "$SKEW"])
            vix3m = idx.get("$VIX3M")
            vix9d = idx.get("$VIX9D")
            vvix = idx.get("$VVIX")
            skew = idx.get("$SKEW")
        except Exception as exc:
            print(f"  note: secondary vol indices unavailable ({exc}).")
        # S5FI ($SPXA50R = S&P 500 % above their 50-day SMA). Schwab's feed doesn't
        # carry the breadth symbol, so compute it from the 500 constituents via
        # yfinance (cached daily). The app classifies the bands + weekly-slope trend.
        s5fi = s5fi_slope_wk = s5fi_weekly = None
        try:
            import s5fi_breadth
            br = s5fi_breadth.get_s5fi(data_dir)
            if br:
                s5fi = br.get("level")
                s5fi_slope_wk = br.get("slopeWk")
                s5fi_weekly = br.get("weekly")
        except Exception as exc:
            print(f"  note: S5FI breadth unavailable ({exc}).")
        # MES — Micro E-mini S&P 500 daily direction tracker. Daily series + slope
        # from yfinance ES=F (cached); live level from the Schwab /MES quote.
        mes = mes_slope_day = mes_daily = None
        try:
            import mes_daily as _mesmod
            m = _mesmod.get_mes_series(data_dir)
            if m:
                mes = m.get("level")
                mes_slope_day = m.get("slopeDay")
                mes_daily = m.get("daily")
        except Exception as exc:
            print(f"  note: MES tracker unavailable ({exc}).")
        try:
            mq = sc.get_quotes(c, ["/MES"])
            if mq.get("/MES") is not None:
                mes = round(float(mq["/MES"]), 2)  # live Micro quote beats the cached close
        except Exception as exc:
            print(f"  note: MES live quote unavailable ({exc}).")
        # 20-day realized vol from SPY closes, so the VIX tab's RV/VRP agree with the
        # Morning Brief (same routine). ~60 calendar days ≈ 40 sessions, plenty for 21.
        rv20 = None
        try:
            spy_closes = [k["close"] for k in sc.get_price_history(c, "SPY", days=60)
                          if k.get("close") is not None]
            rv20 = _realized_vol_pct(spy_closes, 20)
        except Exception as exc:
            print(f"  note: realized vol unavailable ({exc}).")
        vix_payload = {
            "asof": prices_as_of,
            "source": SOURCE_LABEL,
            "inputs": {
                "vix": vix_level,
                "vix9d": vix9d, "vix3m": vix3m, "vvix": vvix, "skew": skew,
                "s5fi": s5fi, "s5fiSlopeWk": s5fi_slope_wk, "s5fiWeekly": s5fi_weekly,
                "mes": mes, "mesSlopeDay": mes_slope_day, "mesDaily": mes_daily,
                "realizedVol20": rv20, "realizedVol30": None,
            },
        }
        with open(os.path.join(data_dir, VIX_FILE), "w", encoding="utf-8") as f:
            json.dump(vix_payload, f, indent=2)

    n_pos = sum(len(d["equities"]) + len(d["options"]) for d in data_by_account.values())
    print(f"Wrote {out_path}  ({len(app_accounts)} account(s), {n_pos} positions).")


if __name__ == "__main__":
    main()
