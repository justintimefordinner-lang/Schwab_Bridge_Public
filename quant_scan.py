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

# The rule, as backtested (combo 87 / "4% target"). Overridable from .env.
PARAMS = {
    "targetYield": float(os.environ.get("QUANT_TARGET_YIELD", "0.04")),  # of strike, per yieldDays
    "yieldDays": int(os.environ.get("QUANT_YIELD_DAYS", "30")),
    "maxDelta": float(os.environ.get("QUANT_MAX_DELTA", "0.35")),
    "expMin": int(os.environ.get("QUANT_EXP_MIN", "28")),
    "expMax": int(os.environ.get("QUANT_EXP_MAX", "45")),
    "closeAtPct": 50,          # the study kept closing at 50% of the credit
    "maxPerTicker": 0.10,      # of buying power; one contract may overshoot to
    "tickerBand": 0.05,        #   maxPerTicker + tickerBand when adding
}
CHAIN_PAUSE_SEC = 0.5  # Schwab allows ~120 calls/min; stay well under


def _data_dir() -> str:
    from research_sync import _app_data_dir
    return _app_data_dir()


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _put_yield(bid: float, strike: float, dte: int) -> float:
    """Premium as a share of the strike, scaled to yieldDays — the study's put_yield."""
    return (bid / strike) * PARAMS["yieldDays"] / max(1, dte)


def _contract(c: dict, strike: float, dte: int, exp: str, delta: float, spot: float) -> dict:
    bid = float(c.get("bid") or 0.0)
    ask = float(c.get("ask") or 0.0)
    mark = float(c.get("mark") or ((bid + ask) / 2 if ask else bid))
    y = _put_yield(bid, strike, dte)
    return {
        "exp": exp, "dte": dte, "strike": round(strike, 2),
        "bid": round(bid, 2), "ask": round(ask, 2), "mark": round(mark, 2),
        "delta": round(delta, 3),
        "yield30": round(y * 100, 2),                       # % of strike per 30 days, at the mid
        "annPct": round((bid / strike) * 365 / max(1, dte) * 100, 1),
        "premium": round(bid * 100, 2),                     # $ per contract at the bid
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

    chain = sc.get_option_chain(c, sym, days=PARAMS["expMax"] + 1, strike_count=60, puts_only=True)
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
        if not (PARAMS["expMin"] <= dte <= PARAMS["expMax"]):
            continue
        for strike_s, lst in strikes.items():
            cdata = lst[0] if isinstance(lst, list) and lst else None
            if not isinstance(cdata, dict):
                continue
            strike = float(strike_s)
            bid = float(cdata.get("bid") or 0.0)
            if bid <= 0 or strike >= spot:
                continue
            dl = am_report._put_delta(cdata, strike, spot, dte)   # Schwab's delta, else from IV
            if dl is None or abs(dl) > PARAMS["maxDelta"]:
                continue
            candidates.append(_contract(cdata, strike, dte, exp, abs(dl), spot))

    if not candidates:
        row["reason"] = "no_puts"
        return row
    paying = [k for k in candidates if k["yield30"] >= PARAMS["targetYield"] * 100]
    # The richest contract under the cap is the "closest miss" when nothing pays.
    row["best"] = max(candidates, key=lambda k: k["yield30"])
    if not paying:
        row["reason"] = "low"
        return row
    # Lowest delta first, then the higher yield: the study's tie-break.
    pick = min(paying, key=lambda k: (k["delta"], -k["yield30"]))
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
