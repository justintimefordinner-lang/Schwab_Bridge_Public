"""
quant_scan.py — the wheel study's put-selection rule, run against live chains.

The QuantConnect study (2022–2026 backtests, 2023 hold-out) settled on one rule
that was better than or equal to the baseline in every period: sell the
LOWEST-delta put whose premium (at the mid) pays at least 4% of the strike per 30 days,
never above 0.35 delta, choosing across every expiration 28–45 days out. The
edge is in skipping underpaid names, not in the delta itself.

This module applies exactly that rule to every approved ticker and writes
APP_DATA_DIR/quant-scan.json: for each name, the contract the rule would sell
(or why it wouldn't), with the figures needed to judge it. Sizing against the
account (per-ticker caps, free cash, VIX-scaled margin) is done by the dashboard,
which knows which account is selected; here it's market data only.

Runs two ways:
  * on demand — the dashboard's "Scan now" drops task_inbox/quant_scan; the
    auto_push loop calls process() every tick and reports through
    APP_DATA_DIR/quant-status.json;
  * on a schedule — QUANT_PUSH_INTERVAL (default hourly), skipped while the market
    is closed so stale off-hours quotes never replace a real scan.

Read-only. ~one chain call per approved name.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone

OUT_FILE = "quant-scan.json"
STATUS_FILE = "quant-status.json"
INBOX_DIR = "task_inbox"
SCAN_MARKER = os.path.join(INBOX_DIR, "quant_scan")

# The rule, as backtested (combo 87 / "4% target"). The trader always follows
# this one: every row carries its pick as `study`.
STUDY = {
    "targetYield": 0.04,       # of strike, per yieldDays
    "yieldDays": 30,
    "maxDelta": 0.35,
    "expMin": 28,
    "expMax": 45,
    "closeAtPct": 50,          # the study kept closing at 50% of the credit
    "maxPerTicker": 0.10,      # of buying power; one contract may overshoot to
    "tickerBand": 0.05,        #   maxPerTicker + tickerBand when adding
}
# What the dashboard's Quant page shows: the study's values, overridable from
# .env, and on top of that the user's choices from the page's Settings
# (data/quant-settings.json), re-read at the start of every scan.
PARAMS = {
    **STUDY,
    "targetYield": float(os.environ.get("QUANT_TARGET_YIELD", STUDY["targetYield"])),
    "yieldDays": int(os.environ.get("QUANT_YIELD_DAYS", STUDY["yieldDays"])),
    "maxDelta": float(os.environ.get("QUANT_MAX_DELTA", STUDY["maxDelta"])),
    "expMin": int(os.environ.get("QUANT_EXP_MIN", STUDY["expMin"])),
    "expMax": int(os.environ.get("QUANT_EXP_MAX", STUDY["expMax"])),
}
_ENV_PARAMS = dict(PARAMS)
SETTINGS_FILE = "quant-settings.json"
_RANGES = {"targetYield": (0.005, 0.2), "yieldDays": (7, 90), "maxDelta": (0.05, 0.6), "expMin": (1, 180), "expMax": (1, 180),
           "closeAtPct": (10, 95), "maxPerTicker": (0.01, 0.5), "tickerBand": (0.0, 0.25)}
CHAIN_PAUSE_SEC = 0.5  # Schwab allows ~120 calls/min; stay well under


def load_settings(data_dir: str) -> bool:
    """Apply the dashboard's Quant settings on top of the .env defaults. Values out
    of range are ignored one by one. Returns True when PARAMS differ from STUDY."""
    raw = _read_json(os.path.join(data_dir, SETTINGS_FILE), None)
    merged = dict(_ENV_PARAMS)
    if isinstance(raw, dict):
        for k, (lo, hi) in _RANGES.items():
            v = raw.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                merged[k] = int(round(v)) if k in ("yieldDays", "expMin", "expMax", "closeAtPct") else float(v)
    if merged["expMin"] > merged["expMax"]:
        merged["expMin"], merged["expMax"] = _ENV_PARAMS["expMin"], _ENV_PARAMS["expMax"]
    PARAMS.clear()
    PARAMS.update(merged)
    return PARAMS != STUDY


def _choose(candidates: list[dict], params: dict) -> tuple[dict | None, dict | None]:
    """The rule under `params`: among puts inside its expiry window and under its
    delta cap, the lowest delta paying the target (ties to the higher yield); the
    richest contract is the closest miss. Yields are re-scaled to its yieldDays."""
    inwin = []
    for k in candidates:
        if not (params["expMin"] <= k["dte"] <= params["expMax"]) or k["delta"] > params["maxDelta"]:
            continue
        y = (k["mark"] / k["strike"]) * params["yieldDays"] / max(1, k["dte"]) * 100 if k["strike"] else 0.0
        inwin.append({**k, "yield30": round(y, 2)})
    if not inwin:
        return None, None
    best = max(inwin, key=lambda k: k["yield30"])
    paying = [k for k in inwin if k["yield30"] >= params["targetYield"] * 100]
    pick = min(paying, key=lambda k: (k["delta"], -k["yield30"])) if paying else None
    return pick, best


def _data_dir() -> str:
    from research_sync import _app_data_dir
    return _app_data_dir()


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _put_yield(premium: float, strike: float, dte: int) -> float:
    """Premium as a share of the strike, scaled to yieldDays — the study's put_yield, taken at the mid."""
    return (premium / strike) * PARAMS["yieldDays"] / max(1, dte)


def _contract(c: dict, strike: float, dte: int, exp: str, delta: float, spot: float) -> dict:
    bid = float(c.get("bid") or 0.0)
    ask = float(c.get("ask") or 0.0)
    mark = float(c.get("mark") or ((bid + ask) / 2 if ask else bid))
    y = _put_yield(mark, strike, dte)  # at the mid, about where a working limit order fills
    return {
        "exp": exp, "dte": dte, "strike": round(strike, 2),
        "bid": round(bid, 2), "ask": round(ask, 2), "mark": round(mark, 2),
        "delta": round(delta, 3),
        "yield30": round(y * 100, 2),                       # % of strike per 30 days, at the mid
        "annPct": round((mark / strike) * 365 / max(1, dte) * 100, 1),
        "premium": round(mark * 100, 2),                    # $ per contract at the mid
        "collateral": round(strike * 100, 2),
        "oi": int(c.get("openInterest") or 0),
        "volume": int(c.get("totalVolume") or 0),
        "spreadPct": round(((ask - bid) / mark) * 100, 1) if mark and ask else None,
        "iv": round(float(c.get("volatility") or 0) / 100, 4) or None,
        "belowSpotPct": round((1 - strike / spot) * 100, 1) if spot else None,
    }


def scan_symbol(c, sym: str, earnings: dict) -> dict:
    """Apply the rule to one name. Never raises: a bad chain is a row with a reason."""
    import am_report
    import schwab_client as sc

    today = date.today()
    row: dict = {"sym": sym, "price": None, "pick": None, "best": None, "reason": "no_chain",
                 "erDate": earnings.get(sym), "erDays": None, "erInWindow": False}
    er = earnings.get(sym)
    if er:
        try:
            row["erDays"] = (date.fromisoformat(er) - today).days
        except ValueError:
            pass

    chain = sc.get_option_chain(c, sym, days=max(PARAMS["expMax"], STUDY["expMax"]) + 1, strike_count=60, puts_only=True)
    if not chain:
        return row
    spot = chain.get("underlyingPrice") or (chain.get("underlying") or {}).get("last")
    spot = float(spot) if spot else None
    row["price"] = round(spot, 2) if spot else None
    if not spot:
        return row

    candidates: list[dict] = []   # every put in the window under the delta cap
    for exp_key, strikes in (chain.get("putExpDateMap") or {}).items():
        exp = exp_key.split(":")[0]
        try:
            dte = (date.fromisoformat(exp) - today).days
        except ValueError:
            continue
        if not (min(PARAMS["expMin"], STUDY["expMin"]) <= dte <= max(PARAMS["expMax"], STUDY["expMax"])):
            continue  # wide enough for both rules; _choose narrows per rule
        for strike_s, lst in strikes.items():
            cdata = lst[0] if isinstance(lst, list) and lst else None
            if not isinstance(cdata, dict):
                continue
            strike = float(strike_s)
            bid = float(cdata.get("bid") or 0.0)
            if bid <= 0 or strike >= spot:
                continue
            dl = am_report._put_delta(cdata, strike, spot, dte)   # Schwab's delta, else from IV
            if dl is None or abs(dl) > max(PARAMS["maxDelta"], STUDY["maxDelta"]):
                continue
            candidates.append(_contract(cdata, strike, dte, exp, abs(dl), spot))

    if not candidates:
        row["reason"] = "no_puts"
        return row
    pick, best = _choose(candidates, PARAMS)
    row["best"] = best  # the closest miss when nothing pays
    # The backtest's rule, always, for the trader (the same put when the settings are the study's).
    row["study"] = pick if PARAMS == STUDY else _choose(candidates, STUDY)[0]
    if not pick:
        row["reason"] = "low"
        return row
    row["pick"] = pick
    row["reason"] = "ok"
    if row["erDays"] is not None:
        row["erInWindow"] = 0 <= row["erDays"] <= pick["dte"]
    return row


def scan(force: bool = False) -> dict:
    import am_report
    import schwab_client as sc
    from research_sync import load_approved

    data_dir = _data_dir()
    custom = load_settings(data_dir)
    is_open, _ = am_report._market_status()
    out_path = os.path.join(data_dir, OUT_FILE)
    if not force and is_open is False and os.path.exists(out_path):
        raise SystemExit("market closed — keeping the last scan (Scan now forces one)")

    approved = load_approved(data_dir)
    earnings = _read_json(os.path.join(data_dir, "earnings.json"), {}) or {}
    c = sc.get_client()

    rows = []
    t0 = time.time()
    for sym in approved:
        try:
            rows.append(scan_symbol(c, sym, earnings))
        except Exception as exc:  # noqa: BLE001 — one bad name must not sink the scan
            rows.append({"sym": sym, "price": None, "pick": None, "best": None, "reason": f"error: {exc}"[:120],
                         "erDate": earnings.get(sym), "erDays": None, "erInWindow": False})
        time.sleep(CHAIN_PAUSE_SEC)

    # Picks first, richest yield at the top; then the rest by how close they came.
    rows.sort(key=lambda r: (r["pick"] is None, -(r["pick"] or r["best"] or {}).get("yield30", 0)))
    payload = {
        "meta": {
            "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "marketOpen": is_open,
            "universe": len(approved),
            "qualifying": sum(1 for r in rows if r["pick"]),
            "params": PARAMS,
            "study": STUDY,
            "custom": custom,
            "source": "schwab-bridge",
            "elapsedSec": round(time.time() - t0, 1),
        },
        "rows": rows,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, out_path)
    print(f"quant scan: {payload['meta']['qualifying']} of {len(approved)} names pay the target "
          f"({payload['meta']['elapsedSec']}s)")
    return payload


def main() -> None:
    """Scheduled entry point (auto_push target). Skips while the market is closed."""
    scan(force=False)


# ---- app-triggered "Scan now": marker in, status out ----------------------------
def _write_status(status: str, error: str | None = None) -> None:
    try:
        path = os.path.join(_data_dir(), STATUS_FILE)
    except SystemExit:
        return
    payload = {"status": status, "error": error,
               "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


def process(log=None) -> None:
    """Service a dashboard-dropped scan request. Safe to call every tick."""
    if not os.path.exists(SCAN_MARKER):
        return
    try:
        os.remove(SCAN_MARKER)
    except OSError:
        pass
    _write_status("running")
    if log:
        log("quant scan: requested from the app — scanning the approved list")
    try:
        scan(force=True)
        _write_status("done")
        if log:
            log("quant scan: done")
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — surface any failure to the UI
        msg = str(exc) or exc.__class__.__name__
        _write_status("error", error=msg)
        if log:
            log(f"quant scan: ERROR — {msg}")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    scan(force="--force" in sys.argv)
