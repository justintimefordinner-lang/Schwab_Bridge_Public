#!/usr/bin/env python3
"""
am_report.py — pre-open market briefing, computed entirely from Schwab data.

Writes data/am_report.json for the app's Briefing tab. Four blocks:

  • REGIME  — VIX + VIX3M term structure, vol-weather (deploy/hold), overnight
              /ES /NQ futures, and the VIX cash band (reused from schwab_client).
  • BOARD   — the approved roster run through wheel-entry gates (1.5y uptrend,
              premium floor at ~30Δ/30DTE, tradeable chain), scored, and tiered
              S/A/B on setup quality + VRP + gamma support.
  • VRP     — implied-vs-realized vol per name, summarised as a heat map by group.
  • GAMMA   — naive dealer gamma profile per board name: flip, call wall, put wall.

Everything here is market data only — it never touches the Accounts API. Flow /
dark-pool (the Unusual-Whales-only layer) is intentionally omitted.

    python am_report.py        # build + write data/am_report.json
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone, date, timedelta

import schwab_client as sc
from research_sync import _app_data_dir, load_approved

REPORT_FILE = "am_report.json"
EARNINGS_FILE = "earnings.json"        # {SYM: "YYYY-MM-DD"} next earnings date
IV_HISTORY_FILE = "iv_history.json"    # {SYM: [{"d": "YYYY-MM-DD", "iv": 0.42}, ...]}
SOURCE = "schwab"

CONFIG = {
    "dte_target": 30,
    "dte_window": (25, 45),
    "trade_delta": 0.30,
    "premium_floor_pct": 1.5,   # hard gate: min % return at ~30Δ / ~30DTE
    "oi_min": 50,               # tradeability sanity
    "spread_max_pct": 25.0,
    "oi_ref": 2000,             # full liquidity credit at/above this OI
    "vrp_rich": 1.20,           # IV/RV at/above = rich premium
    "vrp_thin": 0.90,           # IV/RV at/below = thin premium
    "board_show": 12,
    "landmine_days": 10,        # earnings within N calendar days (~7 sessions) = off-board
    "run_pre_open_min": 30,     # full pull is allowed starting N min before the 9:30 open
    "run_post_close_min": 10,   # ...and until N min after the close, then it keeps the last board
    "ladder_base_sec": 300,     # ladder refresh cadence on a calm tape
    "ladder_fast_sec": 90,      # ...tightened to this when the tape is moving (stress)
    "ladder_vix_hot": 22.0,     # VIX at/above this = stress
    "ladder_ivts_hot": 1.0,     # VIX/VIX3M above this (backwardation) = stress
    "ladder_spy_move_hot": 1.0, # |SPY intraday %| at/above this = stress
    "ladder_throttle_sec": 0.15,  # small pause between per-name chain pulls (rate-limit hygiene)
    "ivr_min_samples": 20,      # below this many logged IV points, IVR reads "building"
    "ivr_cap": 260,             # keep ~1y of daily IV per name
    "score_weights": {"trend": 0.40, "vrp": 0.28, "liq": 0.22, "beta": 0.10},
}

# Compact correlation-group map for the VRP heat map (extend freely).
GROUPS = {
    "Semis": {"AMD", "NVDA", "AVGO", "TSM", "MU", "ASML", "LRCX", "AMAT", "ADI",
              "APH", "COHR", "CRDO", "SMH", "TER", "STX", "WDC", "INTC", "GLW", "DRAM"},
    "Mega-cap tech": {"AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "PLTR", "APP"},
    "Financials": {"JPM", "AXP", "SOFI", "HOOD", "FUTU"},
    "Energy/Power": {"CEG", "VRT", "GE", "EQT", "CAT", "ANET", "CLS", "DELL", "NBIS", "IREN"},
    "Miners/Materials": {"CCJ", "NEM", "FCX", "AA", "HL", "CDE", "GLW"},
    "Crypto-linked": {"IBIT", "ETHA", "COIN", "MARA", "RIOT"},
}


def group_of(sym: str) -> str:
    for g, members in GROUPS.items():
        if sym in members:
            return g
    return "Other"


# ----------------------------------------------------------------- data files
def load_earnings(data_dir: str) -> dict[str, str]:
    """Next-earnings dates {SYM: 'YYYY-MM-DD'} from data/earnings.json. Returns {}
    if absent — the engine then simply runs without an earnings gate (and flags it
    in meta) rather than silently pretending names are earnings-safe."""
    path = os.path.join(data_dir, EARNINGS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {str(k).upper(): str(v) for k, v in raw.items() if v}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def _load_iv_history(data_dir: str) -> dict[str, list[dict]]:
    path = os.path.join(data_dir, IV_HISTORY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_iv_history(data_dir: str, hist: dict[str, list[dict]]) -> None:
    path = os.path.join(data_dir, IV_HISTORY_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f)


def _log_iv(hist: dict, sym: str, iv: float | None, day: str) -> None:
    """Append today's ATM IV for `sym` (one sample per date; cap ~1y)."""
    if iv is None:
        return
    series = hist.setdefault(sym, [])
    if series and series[-1].get("d") == day:
        series[-1]["iv"] = round(iv, 4)
    else:
        series.append({"d": day, "iv": round(iv, 4)})
    if len(series) > CONFIG["ivr_cap"]:
        del series[: len(series) - CONFIG["ivr_cap"]]


def iv_rank(series: list[dict], current_iv: float | None) -> tuple[float | None, int]:
    """IV Rank 0–100 of `current_iv` within the logged 52-week IV range. Returns
    (rank, sample_count); rank is None until enough samples accumulate. Unlike VRP
    (IV vs realized), this is IV vs its OWN past year — the lens the OTU scanner uses."""
    ivs = [p["iv"] for p in series if p.get("iv") is not None]
    if current_iv is not None:
        ivs = ivs + [current_iv]
    n = len(ivs)
    if current_iv is None or n < CONFIG["ivr_min_samples"]:
        return None, n
    lo, hi = min(ivs), max(ivs)
    if hi <= lo:
        return 50.0, n
    return round((current_iv - lo) / (hi - lo) * 100, 0), n


def earnings_days(date_str: str | None, today: date) -> int | None:
    """Calendar days until next earnings (None if unknown; negative means past)."""
    if not date_str:
        return None
    try:
        return (date.fromisoformat(date_str[:10]) - today).days
    except ValueError:
        return None


# --------------------------------------------------------------------------- math
def closes_of(candles: list[dict]) -> list[float]:
    return [float(c["close"]) for c in candles if c.get("close") is not None]


def volumes_of(candles: list[dict]) -> list[float]:
    return [float(c.get("volume") or 0) for c in candles]


def rel_vol(volumes: list[float], n: int = 20) -> float | None:
    """Latest session's volume vs its trailing n-day average (RELVOL). >1 = busier
    than usual. Pre-open/closed this reflects the last completed session; intraday it
    grows as today's bar fills."""
    if len(volumes) < n + 1:
        return None
    base = volumes[-(n + 1):-1]
    avg = sum(base) / len(base)
    return round(volumes[-1] / avg, 2) if avg > 0 else None


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def stdev(values: list[float], n: int) -> float | None:
    """Population standard deviation of the last n values (StockCharts BB convention)."""
    if len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window) / n
    return math.sqrt(sum((v - mean) ** 2 for v in window) / n)


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    """20-day Bollinger bands: SMA(n) ± k·σ(n). None until n closes exist."""
    mid = sma(closes, n)
    sd = stdev(closes, n)
    if mid is None or sd is None:
        return None
    return {"mid": round(mid, 2), "sd": round(sd, 4),
            "upper": round(mid + k * sd, 2), "lower": round(mid - k * sd, 2)}


def _strike_bb(strike: float, bands: dict | None) -> dict | None:
    """Where a strike sits on the underlying's Bollinger scale: σ from the 20-day
    mean, %B (0 = lower band, 1 = upper band), and a zone label. For a short put,
    further below the mean (more negative σ) is a deeper, more mean-reversion-friendly
    strike; near the mean is close to the money."""
    if not bands:
        return None
    sd, mid = bands.get("sd"), bands.get("mid")
    lo, up = bands.get("lower"), bands.get("upper")
    if not sd or sd <= 0 or mid is None:
        return None
    sigma = (strike - mid) / sd
    pctb = (strike - lo) / (up - lo) if (up is not None and lo is not None and up != lo) else None
    if sigma <= -2:
        zone = "below lower"
    elif sigma <= -1:
        zone = "lower band"
    elif sigma < 1:
        zone = "mid"
    elif sigma < 2:
        zone = "upper band"
    else:
        zone = "above upper"
    return {"bbSigma": round(sigma, 2),
            "pctB": round(pctb, 2) if pctb is not None else None,
            "bbZone": zone}


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    """Annualised close-to-close volatility over the last n sessions."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _s5fi_weekly_slope(candles: list[dict], weeks: int = 8) -> float | None:
    """Least-squares slope of $SPXA50R weekly closes (level-points per week) over
    the last `weeks`. Daily candles grouped by ISO week → each week's last close →
    fit close ~ week_index. The app classifies steep-up / flat / steep-down.
    None with fewer than 3 weeks of data."""
    by_week: dict[tuple[int, int], float] = {}
    for k in candles:
        ts, close = k.get("datetime"), k.get("close")
        if ts is None or close is None:
            continue
        iso = datetime.utcfromtimestamp(ts / 1000.0).isocalendar()
        by_week[(iso[0], iso[1])] = float(close)
    series = [by_week[w] for w in sorted(by_week)][-weeks:]
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


def trend_metrics(closes: list[float]) -> dict | None:
    """200DMA gate + strength + Bollinger-mid confirmation."""
    if len(closes) < 200:
        return None
    last = closes[-1]
    sma200 = sma(closes, 200)
    sma200_prev = sma(closes[:-21], 200) if len(closes) > 221 else None
    rising_200 = sma200_prev is not None and sma200 > sma200_prev
    base = closes[-378] if len(closes) >= 378 else closes[0]
    ret_18mo = (last / base - 1.0) * 100
    above_200 = last > sma200
    uptrend = above_200 and rising_200 and ret_18mo > 0
    sma20 = sma(closes, 20)
    boll_up = sma20 is not None and last > sma20
    pct_above_200 = (last / sma200 - 1.0) * 100
    strength = max(0.0, min(100.0, 0.5 * min(ret_18mo, 100) + 1.5 * min(pct_above_200, 33)))
    return {"uptrend": uptrend, "ret18mo": round(ret_18mo, 1), "above200": above_200,
            "rising200": rising_200, "bollUp": boll_up,
            "pctAbove200": round(pct_above_200, 1), "strength": round(strength, 1)}


def beta_vs(closes: list[float], spy: list[float]) -> float | None:
    n = min(len(closes), len(spy), 252)
    if n < 60:
        return None
    s, m = closes[-n:], spy[-n:]
    rs = [s[i] / s[i - 1] - 1 for i in range(1, n) if s[i - 1] > 0]
    rm = [m[i] / m[i - 1] - 1 for i in range(1, n) if m[i - 1] > 0]
    k = min(len(rs), len(rm))
    rs, rm = rs[-k:], rm[-k:]
    if k < 30:
        return None
    mean_m, mean_s = sum(rm) / k, sum(rs) / k
    var_m = sum((x - mean_m) ** 2 for x in rm) / k
    cov = sum((rs[i] - mean_s) * (rm[i] - mean_m) for i in range(k)) / k
    return (cov / var_m) if var_m else None


def vrp_flag(iv: float | None, rv: float | None) -> tuple[str, float | None]:
    if not iv or not rv or rv <= 0:
        return "n/a", None
    ratio = iv / rv
    if ratio >= CONFIG["vrp_rich"]:
        return "rich", ratio
    if ratio <= CONFIG["vrp_thin"]:
        return "thin", ratio
    return "fair", ratio


# --------------------------------------------------------------------------- chain
def _near_monthly_exp(exp_map: dict) -> str | None:
    lo, hi = CONFIG["dte_window"]
    from datetime import date
    cands = []
    for key in exp_map:
        ed = date.fromisoformat(key.split(":")[0])
        dte = (ed - date.today()).days
        if lo <= dte <= hi + 7:
            cands.append((key, dte, ed))
    if not cands:
        return None
    monthlies = [c for c in cands if c[2].weekday() == 4 and 15 <= c[2].day <= 21]
    pool = monthlies if monthlies else cands
    return min(pool, key=lambda c: abs(c[1] - CONFIG["dte_target"]))[0]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _put_delta(c: dict, strike: float, spot: float | None, dte: int) -> float | None:
    """A put's delta: Schwab's per-contract value when present, else a Black-Scholes
    estimate from the contract's IV. Schwab leaves delta blank (-999 / None) on thin
    strikes, which is what makes a naive closest-delta pick collapse onto one strike."""
    dl = c.get("delta")
    if dl not in (None, -999, -999.0):
        return dl
    iv = c.get("volatility")  # Schwab carries annualized IV in percent
    if iv and iv > 0 and spot and spot > 0 and strike > 0 and dte > 0:
        sigma = iv / 100.0
        t = dte / 365.0
        d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
        return _norm_cdf(d1) - 1.0   # put delta (negative)
    return None


def _leg_from(c: dict, strike: float, delta: float) -> dict:
    mark = c.get("mark") or (((c.get("bid") or 0) + (c.get("ask") or 0)) / 2)
    prem_pct = (mark / strike * 100) if strike else 0.0
    bid, ask = c.get("bid"), c.get("ask")
    spread_pct = (((ask - bid) / mark) * 100) if (
        mark and bid is not None and ask is not None) else 999.0
    return {"strike": round(strike, 2), "delta": round(delta, 2),
            "mark": round(mark, 2), "premPct": round(prem_pct, 2),
            "oi": int(c.get("openInterest") or 0), "spreadPct": round(spread_pct, 1)}


def put_ladder(chain: dict, bands: dict | None = None) -> list[dict]:
    """The ~30 / 25 / 20-delta puts in the near-monthly expiration, each with premium
    %, OI, spread, and annualized return (premPct × 365/DTE). Picks the strike whose
    delta is closest to each target and forces three DISTINCT strikes, so the ladder
    can't collapse onto one contract when strikes are coarse or Schwab's per-strike
    deltas are sparse (it backfills those from IV). When `bands` (20-day Bollinger) is
    supplied, each leg also carries where its strike sits on that scale. First leg
    (30Δ) is the gate ref."""
    from datetime import date
    pmap = chain.get("putExpDateMap") or {}
    exp = _near_monthly_exp(pmap)
    if not exp:
        return []
    dte = (date.fromisoformat(exp.split(":")[0]) - date.today()).days
    exp_label = exp.split(":")[0]
    spot = chain.get("underlyingPrice")

    # All strikes with a usable (real or IV-derived) delta.
    cands = []
    for strike_s, lst in pmap[exp].items():
        c = lst[0]
        strike = float(strike_s)
        dl = _put_delta(c, strike, spot, dte)
        if dl is None:
            continue
        cands.append((strike, dl, c))
    if not cands:
        return []

    legs = []
    used: set[float] = set()
    for dtarget in (0.30, 0.25, 0.20):
        target = -dtarget
        pool = [t for t in cands if t[0] not in used] or cands  # keep strikes distinct
        strike, dl, c = min(pool, key=lambda t: abs(t[1] - target))
        used.add(strike)
        leg = _leg_from(c, strike, dl)
        leg["dTarget"] = int(round(dtarget * 100))
        leg["dte"] = dte
        leg["exp"] = exp_label
        leg["annPct"] = round(leg["premPct"] * 365 / dte, 1) if dte > 0 else None
        bb = _strike_bb(strike, bands)
        if bb:
            leg.update(bb)
        legs.append(leg)
    return legs


def gamma_profile(chain: dict, spot: float | None) -> dict | None:
    """Naive dealer gamma: per strike, call gamma×OI is positive, put gamma×OI
    negative (standard 'dealers short customer puts' convention). Returns the
    zero-gamma flip strike, the highest-call-OI wall (resistance) and the
    highest-put-OI wall (support). Aggregates the fetched near-term window."""
    if not spot:
        return None
    call_oi: dict[float, float] = {}
    put_oi: dict[float, float] = {}
    gex: dict[float, float] = {}
    for side, sign, oi_book in (("callExpDateMap", 1.0, call_oi),
                                ("putExpDateMap", -1.0, put_oi)):
        for _exp, strikes in (chain.get(side) or {}).items():
            for strike, lst in strikes.items():
                c = lst[0]
                k = round(float(strike), 2)
                oi = float(c.get("openInterest") or 0)
                gm = c.get("gamma")
                if gm in (None, -999, -999.0):
                    gm = 0.0
                oi_book[k] = oi_book.get(k, 0.0) + oi
                gex[k] = gex.get(k, 0.0) + sign * float(gm) * oi * 100 * spot * spot * 0.01
    if not gex:
        return None
    strikes_sorted = sorted(gex)
    # flip = strike where cumulative GEX crosses zero
    cum = 0.0
    flip = None
    prev_k = None
    for k in strikes_sorted:
        nxt = cum + gex[k]
        if prev_k is not None and (cum <= 0 < nxt or cum >= 0 > nxt):
            flip = k
            break
        cum, prev_k = nxt, k
    call_wall = max(call_oi, key=call_oi.get) if call_oi else None
    put_wall = max(put_oi, key=put_oi.get) if put_oi else None
    net = sum(gex.values())
    return {"flip": round(flip, 2) if flip else None,
            "callWall": round(call_wall, 2) if call_wall else None,
            "putWall": round(put_wall, 2) if put_wall else None,
            "net": "pos" if net >= 0 else "neg"}


def atm_iv(chain: dict, spot: float | None) -> float | None:
    """ATM implied vol (decimal) from the nearest put strike to spot."""
    if not spot:
        return None
    pmap = chain.get("putExpDateMap") or {}
    exp = _near_monthly_exp(pmap)
    if not exp:
        return None
    best_k, best_iv = None, None
    for strike, lst in pmap[exp].items():
        k = float(strike)
        iv = lst[0].get("volatility")
        if iv in (None, -999, -999.0):
            continue
        if best_k is None or abs(k - spot) < abs(best_k - spot):
            best_k, best_iv = k, float(iv)
    return (best_iv / 100.0) if best_iv else None   # Schwab IV is in percent


# --------------------------------------------------------------------------- regime
def build_regime(quotes: dict[str, dict]) -> dict:
    def last(sym):
        q = quotes.get(sym) or {}
        for key in ("lastPrice", "mark", "closePrice", "last"):
            if q.get(key) is not None:
                return float(q[key])
        return None

    vix = last("$VIX")
    vix3m = last("$VIX3M")
    band = sc.vix_band(vix) if vix is not None else None
    term = None
    if vix is not None and vix3m is not None:
        term = "backwardation" if vix > vix3m else "contango"
    # vol-weather: deploy when term structure is calm AND vix isn't elevated
    elevated = vix is not None and vix >= 20
    weather = "hold" if (term == "backwardation" or elevated) else "deploy"

    futures = []
    for sym, label in (("/ES", "ES"), ("/NQ", "NQ")):
        q = quotes.get(sym) or {}
        lastp = q.get("lastPrice") or q.get("mark")
        prev = q.get("closePrice")
        if lastp and prev:
            futures.append({"sym": label, "pct": round((lastp / prev - 1) * 100, 2)})

    return {
        "vix": round(vix, 2) if vix is not None else None,
        "vix3m": round(vix3m, 2) if vix3m is not None else None,
        "termStructure": term,
        "band": band["regime"] if band else None,
        "cashRange": band["cash"] if band else None,
        "volWeather": weather,
        "futures": futures,
        "s5fi": None,         # filled by the caller from s5fi_breadth (computed)
        "s5fiSlopeWk": None,
    }


# --------------------------------------------------------------------------- screen
def screen(sym: str, candles: list[dict], spy: list[float], chain: dict | None,
           spot: float | None, earnings: dict[str, str] | None = None,
           iv_series: list[dict] | None = None, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    closes = closes_of(candles)
    tm = trend_metrics(closes)
    if not tm:
        return {"sym": sym, "skip": True, "fails": ["insufficient history"]}

    move = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 and closes[-2] else None
    rv_vol = rel_vol(volumes_of(candles))
    fails = []
    if not tm["uptrend"]:
        fails.append("no 1.5y uptrend")

    bands = bollinger(closes)
    ladder = put_ladder(chain, bands) if chain else []
    put = ladder[0] if ladder else None   # 30Δ leg = the gate reference
    if not put:
        fails.append("no 30D/30Δ chain")
    else:
        if put["premPct"] < CONFIG["premium_floor_pct"]:
            fails.append("premium<%.1f%%" % CONFIG["premium_floor_pct"])
        if put["oi"] < CONFIG["oi_min"] or put["spreadPct"] > CONFIG["spread_max_pct"]:
            fails.append("untradeable chain")

    iv = atm_iv(chain, spot) if chain else None
    rv = realized_vol(closes)
    vrp, ratio = vrp_flag(iv, rv)
    beta = beta_vs(closes, spy) if spy else None
    gamma = gamma_profile(chain, spot) if chain else None
    ivr, ivr_n = iv_rank(iv_series or [], iv)

    er_days = earnings_days((earnings or {}).get(sym), today)
    # does the board put (≈30 DTE) span the earnings event?
    er_spans_put = False
    if er_days is not None and er_days >= 0 and put:
        er_spans_put = er_days <= put["dte"]

    # score (setup quality first)
    w = CONFIG["score_weights"]
    trend_score = min(100.0, tm["strength"] + (12 if tm["bollUp"] else 0))
    vrp_score = 100.0 if vrp == "rich" else (55.0 if vrp == "fair" else 0.0)
    liq_score = 0.0
    if put:
        oi_credit = min(1.0, math.log10(max(put["oi"], 1)) / math.log10(CONFIG["oi_ref"]))
        spread_credit = max(0.0, 1 - put["spreadPct"] / 20.0)
        liq_score = (0.6 * oi_credit + 0.4 * spread_credit) * 100
    beta_score = min(100.0, max(0.0, (beta or 0) / 3.5 * 100))
    score = (w["trend"] * trend_score + w["vrp"] * vrp_score
             + w["liq"] * liq_score + w["beta"] * beta_score)

    # tier S/A/B from setup + VRP + gamma support (no flow layer)
    pts = 0
    if vrp == "rich":
        pts += 2
    elif vrp == "thin":
        pts -= 1
    if tm["bollUp"]:
        pts += 1
    if gamma and gamma.get("net") == "pos":
        pts += 1
    if gamma and gamma.get("putWall") and spot and gamma["putWall"] <= spot:
        pts += 1   # a put-wall floor below price = structural support
    tier = "S" if pts >= 4 else ("A" if pts >= 2 else "B")

    return {
        "sym": sym, "skip": False, "fails": fails,
        "trend": tm, "chain": put, "ladder": ladder, "bb": bands,
        "iv": round(iv, 3) if iv else None,
        "rv": round(rv, 3) if rv else None, "vrp": vrp,
        "vrpRatio": round(ratio, 2) if ratio else None,
        "beta": round(beta, 2) if beta else None, "gamma": gamma,
        "score": round(score, 1), "tier": tier, "group": group_of(sym),
        "move": round(move, 2) if move is not None else None,
        "last": round(closes[-1], 2),
        "relVol": rv_vol,
        "ivr": ivr, "ivrSamples": ivr_n,
        "erDays": er_days, "erDate": (earnings or {}).get(sym),
        "erSpansPut": er_spans_put,
    }


def vrp_heatmap(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("vrpRatio") is not None:
            groups.setdefault(r["group"], []).append(r)
    out = []
    for g, members in groups.items():
        rich = sum(1 for m in members if m["vrp"] == "rich")
        fair = sum(1 for m in members if m["vrp"] == "fair")
        thin = sum(1 for m in members if m["vrp"] == "thin")
        members_sorted = sorted(members, key=lambda m: m["vrpRatio"], reverse=True)
        richest = members_sorted[0]
        out.append({"group": g, "n": len(members), "rich": rich, "fair": fair,
                    "thin": thin, "richest": "%s %.2f" % (richest["sym"], richest["vrpRatio"]),
                    "members": members_sorted})
    out.sort(key=lambda x: (-x["rich"], -x["n"]))
    return out


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm) — for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """NYSE observed rule: Saturday → prior Friday, Sunday → following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _holidays(year: int) -> set[date]:
    h = {
        _observed(date(year, 1, 1)),          # New Year's Day
        _nth_weekday(year, 1, 0, 3),          # MLK Jr. Day
        _nth_weekday(year, 2, 0, 3),          # Washington's Birthday
        _easter(year) - timedelta(days=2),    # Good Friday
        _last_weekday(year, 5, 0),            # Memorial Day
        _observed(date(year, 7, 4)),          # Independence Day
        _nth_weekday(year, 9, 0, 1),          # Labor Day
        _nth_weekday(year, 11, 3, 4),         # Thanksgiving
        _observed(date(year, 12, 25)),        # Christmas
    }
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))   # Juneteenth
    return h


def _early_closes(year: int) -> set[date]:
    """1:00 PM ET early-close sessions (minus any that collide with a full holiday)."""
    ec = set()
    ec.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # day after Thanksgiving
    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 5:
        ec.add(xmas_eve)
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5:
        ec.add(jul3)
    return ec - _holidays(year)


EARLY_CLOSE_LABEL = "1:00 PM ET"
_EARLY_CLOSE_MIN = 13 * 60   # 1:00 PM ET


def _market_status(when: datetime | None = None) -> tuple[bool | None, str | None]:
    """(is_open, early_close_label) for `when` (default now, UTC), honoring weekends,
    NYSE holidays, and 1pm early-close days. is_open is None if the timezone can't be
    resolved (app then omits the indicator). early_close_label is set on any early-close
    day (so the session-end time can be shown even while still open)."""
    when = when or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = when.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return None, None
    d = et.date()
    if et.weekday() >= 5 or d in _holidays(d.year):
        return False, None
    early = d in _early_closes(d.year)
    close_min = _EARLY_CLOSE_MIN if early else 960   # 4:00 PM ET
    mins = et.hour * 60 + et.minute
    is_open = 570 <= mins < close_min                # 9:30 AM ET open
    return is_open, (EARLY_CLOSE_LABEL if early else None)


def _run_window(when: datetime | None = None) -> tuple[bool, str]:
    """Whether the FULL report should pull right now. True during the trading-day
    active window — from run_pre_open_min before the 9:30 open through run_post_close_min
    after the close (4:00 PM, or 1:00 PM on early-close days). False overnight, on
    weekends, and on NYSE holidays, when Schwab doesn't reliably serve option chains
    and a pull would only waste ~115 calls and risk blanking the board. Returns
    (active, reason); reason is a short label for logging. If the timezone can't be
    resolved it returns True (don't block)."""
    when = when or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = when.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return True, "tz-unknown"
    d = et.date()
    if et.weekday() >= 5:
        return False, "weekend"
    if d in _holidays(d.year):
        return False, "holiday"
    early = d in _early_closes(d.year)
    close_min = _EARLY_CLOSE_MIN if early else 960
    pre = CONFIG["run_pre_open_min"]
    post = CONFIG["run_post_close_min"]
    mins = et.hour * 60 + et.minute
    if mins < 570 - pre:
        return False, "pre-market (>%d min before open)" % pre
    if mins > close_min + post:
        return False, "closed (>%d min after close)" % post
    return True, "active"


def _stress_interval(c) -> tuple[int, dict]:
    """Pick the ladder-refresh cadence from live stress signals (one cheap quote call):
    baseline on a calm tape, tightened when the VIX is elevated, the term structure is
    backwardated (VIX > VIX3M), or SPY is moving hard intraday — the conditions under
    which option marks actually drift inside a few minutes. Returns (seconds, signals)."""
    base, fast = CONFIG["ladder_base_sec"], CONFIG["ladder_fast_sec"]
    vix = vix3m = spy_move = None
    try:
        q = sc.get_quotes_raw(c, ["$VIX", "$VIX3M", "SPY"])

        def _last(sym: str):
            qq = q.get(sym) or {}
            return qq.get("lastPrice") or qq.get("mark") or qq.get("closePrice")

        vix = _last("$VIX")
        vix3m = _last("$VIX3M")
        spyq = q.get("SPY") or {}
        last, prev = (spyq.get("lastPrice") or spyq.get("mark")), spyq.get("closePrice")
        if last and prev:
            spy_move = (last / prev - 1) * 100
    except Exception:
        pass
    ivts = (vix / vix3m) if (vix and vix3m) else None
    stressed = bool(
        (vix is not None and vix >= CONFIG["ladder_vix_hot"])
        or (ivts is not None and ivts > CONFIG["ladder_ivts_hot"])
        or (spy_move is not None and abs(spy_move) >= CONFIG["ladder_spy_move_hot"])
    )
    signals = {"vix": vix, "ivts": round(ivts, 3) if ivts else None,
               "spyMovePct": round(spy_move, 2) if spy_move is not None else None,
               "stressed": stressed}
    return (fast if stressed else base), signals


def refresh_ladders(force: bool = False) -> int | None:
    """Light intraday pass: re-pull ONLY the puts chain for names already on the
    board and patch in fresh premiums / annualized yields / VRP. Skips the 600-day
    candle pulls, trend, beta, and gamma (all intraday-static), and leaves board
    membership, tier, and score from the last full run untouched. ~12 small puts-only
    calls instead of ~56 full chains + histories — cheap enough to run every few
    minutes. A full am_report.main() must have built the board first.

    Skips entirely when the market is closed (weekends, NYSE holidays, after the 4pm
    close or a 1pm early close) so it doesn't burn calls re-pulling static prior-close
    marks. Pass force=True (or run `python am_report.py --ladders`) to override. If the
    timezone can't be resolved, it does not block and runs anyway."""
    if not force:
        is_open, _early = _market_status()
        if is_open is False:
            print("Market closed — skipping ladder refresh (use --ladders to force).")
            return

    data_dir = _app_data_dir()
    path = os.path.join(data_dir, REPORT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError):
        print("am_report.json not found — run a full am_report build first.")
        return
    board = report.get("board") or []
    if not board:
        print("No board to refresh.")
        return

    c = sc.get_client()
    updated = 0
    throttle = CONFIG["ladder_throttle_sec"]
    for row in board:
        chain = sc.get_option_chain(c, row["sym"], days=45, strike_count=25, puts_only=True)
        if throttle:
            time.sleep(throttle)   # space the calls so a tight cadence doesn't trip the limiter
        if not chain:
            continue
        ladder = put_ladder(chain, row.get("bb"))
        if not ladder:
            continue
        row["ladder"] = ladder
        row["chain"] = ladder[0]
        # refresh VRP live: fresh ATM IV vs the realized vol from the last full run
        spot = chain.get("underlyingPrice")
        iv = atm_iv(chain, spot) if spot else None
        if iv is not None:
            row["iv"] = round(iv, 3)
            vrp, ratio = vrp_flag(iv, row.get("rv"))
            row["vrp"] = vrp
            row["vrpRatio"] = round(ratio, 2) if ratio else None
        updated += 1

    # Adaptive cadence: how soon should the next refresh run? Tighter when the tape is
    # hot. Stamp the decision + the next-run time so the app can count down to it.
    interval, signals = _stress_interval(c)
    now = datetime.now(timezone.utc)
    meta = report.setdefault("meta", {})
    meta["ladderAsOf"] = now.isoformat(timespec="seconds")
    meta["ladderIntervalSec"] = interval
    meta["ladderNextAt"] = (now + timedelta(seconds=interval)).isoformat(timespec="seconds")
    meta["ladderCadence"] = "fast" if signals["stressed"] else "base"
    meta["ladderStress"] = signals
    mopen, early = _market_status()
    meta["marketOpen"] = mopen
    meta["earlyClose"] = early
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("Refreshed %d/%d board ladders — next in %ds (%s)"
          % (updated, len(board), interval, meta["ladderCadence"]))
    return interval


def main(force: bool = False) -> None:
    data_dir = _app_data_dir()
    # Don't burn ~115 Schwab calls outside the trading-day window — Schwab stops
    # serving option chains off-hours, so a pull would just churn. Keep the last good
    # board instead. --force (or a missing prior report) runs anyway.
    if not force:
        active, why = _run_window()
        if not active:
            report_path = os.path.join(data_dir, REPORT_FILE)
            if os.path.exists(report_path):
                # Keep the last board (no Schwab calls), but refresh the market-status
                # flags on it so the UI shows "Market closed" off-hours. Without this,
                # meta.marketOpen stays True from the last trading-hours run and the
                # closed badge never appears.
                try:
                    mopen, early = _market_status()
                    with open(report_path, encoding="utf-8") as f:
                        prev = json.load(f)
                    meta = prev.setdefault("meta", {})
                    if mopen is not None:
                        meta["marketOpen"] = mopen
                    meta["earlyClose"] = early
                    with open(report_path, "w", encoding="utf-8") as f:
                        json.dump(prev, f, indent=2)
                except Exception as e:
                    print("  (could not refresh market-status flags: %s)" % e)
                print("Market %s — keeping the last board, skipping the pull "
                      "(use --force to run anyway)." % why)
                return
            print("Market %s, but no existing board to keep — building one anyway." % why)
    approved = load_approved(data_dir)
    earnings = load_earnings(data_dir)
    iv_hist = _load_iv_history(data_dir)
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    day_str = today.isoformat()

    c = sc.get_client()

    quotes = sc.get_quotes_raw(c, ["$VIX", "$VIX3M", "/ES", "/NQ"])
    regime = build_regime(quotes)
    # S5FI breadth (Schwab doesn't serve $SPXA50R) — computed from S&P 500
    # constituents via yfinance, cached daily in data/s5fi_cache.json.
    try:
        import s5fi_breadth
        br = s5fi_breadth.get_s5fi(data_dir)
        if br:
            regime["s5fi"] = br.get("level")
            regime["s5fiSlopeWk"] = br.get("slopeWk")
    except Exception as exc:
        print(f"  note: S5FI breadth unavailable ({exc}).")

    spy = closes_of(sc.get_price_history(c, "SPY", days=420))

    rows = []
    for sym in approved:
        candles = sc.get_price_history(c, sym, days=600)   # ~1.5y+ of trading days
        if not candles:
            continue
        spot = closes_of(candles)[-1] if candles else None
        chain = sc.get_option_chain(c, sym)
        row = screen(sym, candles, spy, chain, spot, earnings, iv_hist.get(sym, []), today)
        rows.append(row)
        _log_iv(iv_hist, sym, row.get("iv"), day_str)   # accumulate IV for IVR
    _save_iv_history(data_dir, iv_hist)

    scored = [r for r in rows if not r.get("skip")]

    # earnings landmines: approved names with earnings inside the danger window,
    # pulled OFF the board regardless of how good the setup looks
    lm_days = CONFIG["landmine_days"]
    landmine_syms = {r["sym"] for r in scored
                     if r.get("erDays") is not None and 0 <= r["erDays"] <= lm_days}
    landmines = sorted(
        [{"sym": r["sym"], "erDate": r.get("erDate"), "erDays": r["erDays"]}
         for r in scored if r["sym"] in landmine_syms],
        key=lambda x: x["erDays"])

    board = sorted([r for r in scored if not r["fails"] and r["sym"] not in landmine_syms],
                   key=lambda r: ({"S": 0, "A": 1, "B": 2}[r["tier"]], -r["score"]))[:CONFIG["board_show"]]
    steer_clear = [{"sym": r["sym"], "fails": r["fails"]}
                   for r in scored if r["fails"] and r["sym"] not in landmine_syms]

    def _mover(r: dict) -> dict:
        return {"sym": r["sym"], "move": r["move"], "last": r.get("last"),
                "vrp": r["vrp"], "uptrend": r["trend"]["uptrend"],
                "gated": bool(r["fails"]), "group": r["group"]}
    movable = [r for r in scored if r.get("move") is not None]
    movers = {
        "gainers": [_mover(r) for r in sorted(movable, key=lambda r: r["move"], reverse=True)[:6]],
        "losers": [_mover(r) for r in sorted(movable, key=lambda r: r["move"])[:6]],
    }

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mopen, early = _market_status()
    report = {
        "meta": {"asOf": now_iso, "source": SOURCE, "count": len(approved),
                 "passed": len(board), "ladderAsOf": now_iso,
                 "marketOpen": mopen, "earlyClose": early,
                 "earningsLoaded": bool(earnings), "params": CONFIG},
        "regime": regime,
        "board": board,
        "movers": movers,
        "vrpGroups": vrp_heatmap(scored),
        "landmines": landmines,
        "steerClear": steer_clear,
    }
    # Guard against a data outage blanking the board. Schwab stops serving option
    # chains deep off-hours/weekends (and a transient throttle does the same), which
    # makes every name fail "no 30Δ chain". If NOT ONE name got a usable chain, this
    # run is junk — keep the last good report instead of overwriting it with an empty
    # board. A partial pull still writes (a thin board is legitimate). Mirrors the VIX
    # writer's "only overwrite when we actually got data".
    path = os.path.join(data_dir, REPORT_FILE)
    chain_ok = sum(1 for r in scored if r.get("chain"))
    if chain_ok == 0:
        if os.path.exists(path):
            print("WARNING: 0/%d names returned an option chain — market closed/off-hours "
                  "or rate-limited. Kept the existing %s (board NOT overwritten). "
                  "Re-run during market hours." % (len(scored), REPORT_FILE))
            return
        print("WARNING: 0/%d names returned an option chain (market closed or throttled). "
              "Writing an empty board — no prior report to preserve." % len(scored))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("Wrote %s  (%d board / %d screened / %d approved / %d landmines / earnings=%s)"
          % (path, len(board), len(scored), len(approved), len(landmines),
             "loaded" if earnings else "none"))


if __name__ == "__main__":
    import sys
    if "--ladders" in sys.argv:
        refresh_ladders(force=True)
    else:
        main(force="--force" in sys.argv)
