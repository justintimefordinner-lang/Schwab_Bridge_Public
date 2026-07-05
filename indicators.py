"""Technical indicators computed from daily closes, plus a setup classifier.

Schwab's market-data API gives raw OHLCV candles but no indicators, so we derive
Bollinger %B, RSI(14) and MACD(12/26/9) here ourselves. Pure functions — no
network, fully unit-testable. The research sync folds the live quote in as the
latest close before calling compute_indicators(), so the values reflect *now*.
"""
from __future__ import annotations

from typing import Any

# ---- tunable thresholds ----------------------------------------------------
# These drive the "in zone" ✓ flags shown in the UI. The ranking itself uses the
# continuous gradient sub-scores below (looser anchors), so candidates spread out
# on a 0–100 scale rather than snapping on/off at a single cutoff.
BB_LOW = 0.25      # %B at/below ⇒ "low on the band"
BB_HIGH = 0.75     # %B at/above ⇒ "high on the band"
RSI_OVERSOLD = 40.0
RSI_OVERBOUGHT = 60.0
# MACD direction is taken from the histogram sign (MACD vs its signal line).

# ---- indicator math --------------------------------------------------------
def ema_series(values: list[float], period: int) -> list[float]:
    """Exponential moving average, same length as input (seeded at values[0]).
    With ~1y of history the seed washes out long before the latest bar."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    prev = values[0]
    for v in values[1:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns a series aligned so the last element is the most
    recent RSI; needs at least period+1 closes."""
    if len(closes) < period + 1:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out = [val(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(val(avg_gain, avg_loss))
    return out


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram), each same length as closes."""
    e_fast = ema_series(closes, fast)
    e_slow = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(e_fast, e_slow)]
    signal_line = ema_series(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0):
    """Latest (mid, upper, lower) using a population standard deviation."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var ** 0.5
    return mid, mid + mult * sd, mid - mult * sd


def _round(x: float | None, d: int = 4) -> float | None:
    return None if x is None else round(float(x), d)


def compute_indicators(closes: list[float]) -> dict[str, Any] | None:
    """Latest-bar snapshot of all three indicators from a folded close series.
    Returns None if there isn't enough history to be meaningful."""
    if len(closes) < 35:  # need slow EMA + signal + a prior bar for a cross
        return None
    macd_line, signal_line, hist = macd_series(closes)
    rsis = rsi_series(closes)
    bb = bollinger(closes)
    if not rsis or bb is None:
        return None
    mid, upper, lower = bb
    price = closes[-1]
    pct_b = (price - lower) / (upper - lower) if upper > lower else 0.5
    h_now, h_prev = hist[-1], hist[-2]
    return {
        "price": _round(price, 2),
        "sma20": _round(mid, 2),
        "bbUpper": _round(upper, 2),
        "bbLower": _round(lower, 2),
        "pctB": _round(pct_b, 4),
        "rsi": _round(rsis[-1], 2),
        "macd": _round(macd_line[-1], 4),
        "signal": _round(signal_line[-1], 4),
        "hist": _round(h_now, 4),
        "histPrev": _round(h_prev, 4),
        "macdBullish": h_now > 0,           # MACD above its signal line
        "macdBearish": h_now < 0,
        "freshBullCross": h_prev <= 0 < h_now,  # crossed up on the latest bar
        "freshBearCross": h_prev >= 0 > h_now,
    }


# ---- setup classifier ------------------------------------------------------
# Each indicator contributes a 0..1 sub-score (a gradient, not an on/off flag).
# Bullish favors low-band + oversold + a bullish MACD turn (CSP / LEAP / Bull Put);
# bearish is the mirror (Bear Call). Per-vehicle blends weight the inputs by what
# matters for that trade, so each vehicle ranks its own "strongest candidates".
def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _macd_score(hist: float, hist_prev: float, bullish: bool) -> float:
    """Momentum gradient from the histogram: full credit when it's on the right
    side AND accelerating, partial when merely on-side or just turning."""
    if bullish:
        rising = hist > hist_prev
        if hist > 0 and rising:
            return 1.0
        if hist > 0:
            return 0.7
        if rising:
            return 0.4
        return 0.0
    falling = hist < hist_prev
    if hist < 0 and falling:
        return 1.0
    if hist < 0:
        return 0.7
    if falling:
        return 0.4
    return 0.0


def classify(ind: dict[str, Any]) -> dict[str, Any]:
    pct_b = ind["pctB"]
    rsi = ind["rsi"]
    hist = ind["hist"]
    hist_prev = ind["histPrev"] if ind.get("histPrev") is not None else hist

    # Bullish sub-scores (looser anchors so names spread across the scale):
    #   %B: 1.0 at/below 0.10, 0 at/above 0.60   |   RSI: 1.0 at/below 35, 0 at 55
    bb_b = _clamp01((0.60 - pct_b) / 0.50)
    rs_b = _clamp01((55.0 - rsi) / 20.0)
    mc_b = _macd_score(hist, hist_prev, bullish=True)
    # Bearish mirror: %B 0 at/below 0.40, 1.0 at/above 0.90 | RSI 0 at 45, 1.0 at 65
    bb_r = _clamp01((pct_b - 0.40) / 0.50)
    rs_r = _clamp01((rsi - 45.0) / 20.0)
    mc_r = _macd_score(hist, hist_prev, bullish=False)

    # Per-vehicle blends (weights sum to 1). Premium sellers lean on the dip;
    # LEAP buyers lean on the MACD turn confirming the bounce has begun.
    vehicle_scores = {
        "CSP": round(100 * (0.40 * bb_b + 0.40 * rs_b + 0.20 * mc_b)),
        "Bull Put Spread": round(100 * (0.40 * bb_b + 0.40 * rs_b + 0.20 * mc_b)),
        "LEAP": round(100 * (0.25 * bb_b + 0.25 * rs_b + 0.50 * mc_b)),
        "Bear Call Spread": round(100 * (0.40 * bb_r + 0.35 * rs_r + 0.25 * mc_r)),
    }

    bull = round(100 * (bb_b + rs_b + mc_b) / 3)
    bear = round(100 * (bb_r + rs_r + mc_r) / 3)

    direction: str | None = None
    score = 0
    vehicles: list[str] = []
    if bull >= bear and bull >= 40:
        direction, score, vehicles = "bullish", bull, ["CSP", "LEAP", "Bull Put Spread"]
    elif bear > bull and bear >= 40:
        direction, score, vehicles = "bearish", bear, ["Bear Call Spread"]

    signal = None
    if direction:
        signal = {
            "direction": direction,
            "strength": "strong" if score >= 65 else "forming",
            "score": score,
            "vehicles": vehicles,
        }

    return {
        "bullScore": bull,
        "bearScore": bear,
        "vehicleScores": vehicle_scores,
        "bull": {
            "bbLow": pct_b <= BB_LOW,
            "rsiOversold": rsi <= RSI_OVERSOLD,
            "macdBullish": bool(ind["macdBullish"]),
            "sub": {"bb": round(bb_b, 2), "rsi": round(rs_b, 2), "macd": round(mc_b, 2)},
        },
        "bear": {
            "bbHigh": pct_b >= BB_HIGH,
            "rsiOverbought": rsi >= RSI_OVERBOUGHT,
            "macdBearish": bool(ind["macdBearish"]),
            "sub": {"bb": round(bb_r, 2), "rsi": round(rs_r, 2), "macd": round(mc_r, 2)},
        },
        "signal": signal,
    }


PARAMS = {
    "bbPeriod": 20, "bbMult": 2.0, "bbLow": BB_LOW, "bbHigh": BB_HIGH,
    "rsiPeriod": 14, "rsiOversold": RSI_OVERSOLD, "rsiOverbought": RSI_OVERBOUGHT,
    "macdFast": 12, "macdSlow": 26, "macdSignal": 9,
    "strongScore": 65, "formingScore": 40,
}
