"""
pace_car.py — the book against the simplest alternative: a benchmark bought with
the same money on the same days.

For every account with a value history (value-history.json — the daily totals the
exports log), a virtual account holds PACE_BENCHMARK (default SPY) from the
first logged day: it buys benchmark shares with the opening value, buys more with
every deposit and sells on every withdrawal at that day's close, and is marked at
the close every day after. Book change, pace change and the difference are what
the dashboard's P&L page shows; months and weeks come from the same series.

Cash flows come from the account's money-movement transactions (ACH, wire,
cash, electronic fund, journal), walked back to the first logged day on the
first run and refreshed over the last TXN days after that, cached in
APP_DATA_DIR/cash-flows.json. Manual accounts have no feed, so their flows are
none — their pace runs from the first logged value.

Output: APP_DATA_DIR/pace-car.json

    {"meta": {"asOf", "benchmark", "lastClose"},
     "accounts": {"<id>": {"since", "startValue", "flows": [{date, amount, type}],
                           "series": [{"date", "book", "pace", "close"}],
                           "summary": {"book", "pace", "bookGain", "paceGain", "vsPace", "vsPacePct",
                                       "todayBook", "todayPace", "todayVsPace", "flowsIn", "flowsOut"}}}}

Runs as the "pace" target in auto_push (PACE_PUSH_INTERVAL, default hourly).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

OUT_FILE = "pace-car.json"
FLOWS_FILE = "cash-flows.json"
BENCHMARK = (os.environ.get("PACE_BENCHMARK") or "SPY").strip().upper()
FLOW_TYPES = ["ACH_RECEIPT", "ACH_DISBURSEMENT", "CASH_RECEIPT", "CASH_DISBURSEMENT",
              "ELECTRONIC_FUND", "WIRE_IN", "WIRE_OUT", "JOURNAL"]
WINDOW_DAYS = 20       # per request; Schwab's transactions call likes small spans
ROLLING_DAYS = 10      # later runs: re-pull this much and upsert by activityId
RETRIES = 2


def _data_dir() -> str:
    from research_sync import _app_data_dir
    return _app_data_dir()


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _r2(n: float) -> float:
    return round(n * 100) / 100


# ---- cash flows ---------------------------------------------------------------------
def _flow_types(c) -> list:
    out = []
    for name in FLOW_TYPES:
        t = getattr(c.Transactions.TransactionType, name, None)
        if t is not None:
            out.append(t)
    return out


def _flows_window(c, account_hash: str, from_dt: datetime, to_dt: datetime) -> list[dict[str, Any]]:
    types = _flow_types(c)
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            resp = c.get_transactions(account_hash, start_date=from_dt, end_date=to_dt, transaction_types=types)
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRIES:
                time.sleep(2)
    raise last  # type: ignore[misc]


def _flow_of(t: dict[str, Any]) -> dict[str, Any] | None:
    """One transaction as {id, date, amount, type}; None when it moves no cash."""
    try:
        amount = float(t.get("netAmount") or 0.0)
    except (TypeError, ValueError):
        return None
    if not amount:
        return None
    when = str(t.get("tradeDate") or t.get("time") or "")[:10]
    if len(when) != 10:
        return None
    return {"id": t.get("activityId"), "date": when, "amount": _r2(amount), "type": t.get("type"),
            "description": (t.get("description") or "")[:60]}


def sync_flows(c, account_hash: str, since: str, cache: dict[str, Any]) -> list[dict[str, Any]]:
    """The account's cash flows since `since` (YYYY-MM-DD), from the cache plus a
    fresh pull: the whole span on the first run, the last ROLLING_DAYS after."""
    ent = cache.get(account_hash) or {}
    flows: dict[Any, dict[str, Any]] = {f.get("id") or f"{f['date']}|{f['amount']}": f for f in ent.get("flows", [])}
    now = datetime.now(timezone.utc)
    first = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) - timedelta(days=1)
    start = first if not ent.get("asOf") else max(first, now - timedelta(days=ROLLING_DAYS))
    to_dt = now
    while to_dt > start:
        from_dt = max(start, to_dt - timedelta(days=WINDOW_DAYS))
        for t in _flows_window(c, account_hash, from_dt, to_dt):
            f = _flow_of(t)
            if f:
                flows[f.get("id") or f"{f['date']}|{f['amount']}"] = f
        to_dt = from_dt
        time.sleep(0.3)
    out = sorted(flows.values(), key=lambda f: f["date"])
    cache[account_hash] = {"asOf": now.isoformat(timespec="seconds"), "flows": out}
    return [f for f in out if f["date"] >= since]


# ---- the virtual account ------------------------------------------------------------
def pace_series(points: list[dict[str, Any]], flows: list[dict[str, Any]], closes: dict[str, float]) -> list[dict[str, Any]]:
    """Walk the book's daily values; the benchmark account buys with the opening
    value and with every deposit (sells on withdrawals) at that day's close."""
    pts = [p for p in points if p.get("label") and p.get("value") is not None]
    if not pts or not closes:
        return []
    dates = sorted(closes)

    def close_on(d: str) -> float | None:
        # the benchmark's close on d, else the last close before d
        if d in closes:
            return closes[d]
        prior = [x for x in dates if x < d]
        return closes[prior[-1]] if prior else None

    units = 0.0
    series: list[dict[str, Any]] = []
    fi = 0
    flows = sorted(flows, key=lambda f: f["date"])
    for i, p in enumerate(pts):
        d = str(p["label"])[:10]
        c = close_on(d)
        if c is None or c <= 0:
            continue
        if not series:
            units = float(p["value"]) / c  # the opening value buys the benchmark
        else:
            while fi < len(flows) and flows[fi]["date"] <= d:
                if flows[fi]["date"] > series[-1]["date"]:
                    units += float(flows[fi]["amount"]) / c
                fi += 1
        series.append({"date": d, "book": _r2(float(p["value"])), "pace": _r2(units * c), "close": _r2(c)})
    return series


def summarize(series: list[dict[str, Any]], flows: list[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {}
    first, last = series[0], series[-1]
    net_flows = sum(f["amount"] for f in flows if first["date"] < f["date"] <= last["date"])
    book_gain = last["book"] - first["book"] - net_flows
    pace_gain = last["pace"] - first["pace"] - net_flows
    base = first["book"] + max(0.0, net_flows)
    prev = series[-2] if len(series) > 1 else first
    return {
        "book": last["book"], "pace": last["pace"],
        "bookGain": _r2(book_gain), "paceGain": _r2(pace_gain),
        "bookGainPct": _r2(book_gain / base * 100) if base else 0.0,
        "paceGainPct": _r2(pace_gain / base * 100) if base else 0.0,
        "vsPace": _r2(book_gain - pace_gain), "vsPacePct": _r2((book_gain - pace_gain) / base * 100) if base else 0.0,
        "todayBook": _r2(last["book"] - prev["book"]), "todayPace": _r2(last["pace"] - prev["pace"]),
        "todayVsPace": _r2((last["book"] - prev["book"]) - (last["pace"] - prev["pace"])),
        "flowsIn": _r2(sum(f["amount"] for f in flows if f["amount"] > 0)),
        "flowsOut": _r2(-sum(f["amount"] for f in flows if f["amount"] < 0)),
        "days": len(series),
    }


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    import export_to_app as ex
    import schwab_client as sc

    data_dir = _data_dir()
    history = ex.load_history(data_dir)
    if not history:
        print("pace: no value history yet")
        return
    c = sc.get_client()
    first_day = min(str(p["label"])[:10] for pts in history.values() for p in pts if p.get("label"))
    span = (date.today() - date.fromisoformat(first_day)).days + 10
    candles = sc.get_price_history(c, BENCHMARK, days=max(60, span))
    closes: dict[str, float] = {}
    for k in candles:
        try:
            closes[datetime.fromtimestamp(k["datetime"] / 1000, timezone.utc).date().isoformat()] = float(k["close"])
        except (KeyError, TypeError, ValueError):
            continue
    if not closes:
        print(f"pace: no {BENCHMARK} closes")
        return

    flows_cache = _read(os.path.join(data_dir, FLOWS_FILE), {}) or {}
    broker_ids = {a["hash"] for a in (sc.list_accounts(c) or [])}
    out_accounts: dict[str, Any] = {}
    for acct_id, points in history.items():
        pts = [p for p in points if p.get("label")]
        if not pts:
            continue
        since = str(pts[0]["label"])[:10]
        flows: list[dict[str, Any]] = []
        if acct_id in broker_ids:
            try:
                flows = sync_flows(c, acct_id, since, flows_cache)
            except Exception as exc:  # noqa: BLE001 — keep the series even when the feed is down
                print(f"pace: flows unavailable for {acct_id[:6]}… ({exc}); using cached")
                flows = [f for f in (flows_cache.get(acct_id) or {}).get("flows", []) if f["date"] >= since]
        series = pace_series(pts, flows, closes)
        out_accounts[acct_id] = {
            "since": series[0]["date"] if series else since, "startValue": series[0]["book"] if series else None,
            "flows": flows, "series": series, "summary": summarize(series, flows),
        }
    _write(os.path.join(data_dir, FLOWS_FILE), flows_cache)
    _write(os.path.join(data_dir, OUT_FILE), {
        "meta": {"asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "benchmark": BENCHMARK,
                 "lastClose": max(closes) if closes else None},
        "accounts": out_accounts,
    })
    print(f"pace: {BENCHMARK} beside {len(out_accounts)} account(s), {sum(len(a['flows']) for a in out_accounts.values())} cash flows")


if __name__ == "__main__":
    main()
