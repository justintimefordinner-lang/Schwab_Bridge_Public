"""
sync_trade_history.py
=====================

Builds and maintains data/trade-history.json from your Schwab orders. READ-ONLY
(it places/cancels nothing).

  • First run (or --full): backfills your entire order history by walking
    backward in 60-day windows until it runs out, deduped by order ID.
  • Every later run: pulls only the last 7 days and UPSERTS them by order ID, so
    the most recent week is always refreshed/corrected without re-pulling
    everything older.

Run:
    python sync_trade_history.py          # backfill if no file yet, else 7-day update
    python sync_trade_history.py --full   # force a full rebuild

Designed to run once per day after the close (e.g. via Windows Task Scheduler),
NOT every 60 seconds — keep it out of auto_push.

Needs APP_DATA_DIR set in .env (same folder as snapshot.json).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv

HISTORY_FILE = "trade-history.json"
TXNS_FILE = "transactions.json"

# Field names whose values are full account numbers and must never hit disk in the
# clear. We keep the last 4 (e.g. 12345678 -> ••••5678) so a record is still
# traceable to an account without exposing the number. The account is identified
# everywhere else by its opaque hash, so masking this field breaks nothing.
_MASK_KEYS = {"accountNumber"}


def _mask_acct(v: Any) -> Any:
    s = str(v)
    if len(s) <= 4:
        return s
    return "•" * (len(s) - 4) + s[-4:]


def mask_pii(obj: Any) -> Any:
    """Return a deep copy of obj with any account-number fields masked. Leaves the
    in-memory data untouched (only the on-disk JSON is masked)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _MASK_KEYS and isinstance(v, (str, int)):
                out[k] = _mask_acct(v)
            else:
                out[k] = mask_pii(v)
        return out
    if isinstance(obj, list):
        return [mask_pii(x) for x in obj]
    return obj


TXN_LOOKBACK_DAYS = 58        # transactions API only allows ~60 days back per request
BACKFILL_WINDOW_DAYS = 20     # smaller windows = smaller, faster responses
BACKFILL_MAX_WINDOWS = 40     # safety cap (~2 years)
STOP_AFTER_EMPTY = 3          # stop backfill after this many empty windows
ROLLING_DAYS = 2              # rolling order-refresh window (runs frequently, so 2d is plenty)
TXN_ROLLING_DAYS = 3          # rolling transaction window for frequent (non-deep) runs
REQUEST_TIMEOUT = 60          # seconds; default 30 was too short for busy windows
WINDOW_RETRIES = 2            # retry a window this many times on timeout


def _data_dir() -> str:
    d = os.environ.get("APP_DATA_DIR")
    if not d or not os.path.isdir(d):
        raise SystemExit("Set APP_DATA_DIR in .env to the app's data folder (with snapshot.json).")
    return d


def _is_executed(o: dict[str, Any]) -> bool:
    """True only for orders that actually traded — excludes canceled, replaced,
    rejected, expired, and still-working orders. This is what matches your
    Schwab 'trade records' count."""
    if (o.get("status") or "").upper() == "FILLED":
        return True
    return (o.get("filledQuantity") or 0) > 0


def _fill_price(o: dict[str, Any]) -> float | None:
    """Average/last execution price if the order filled, else None."""
    prices = []
    for act in o.get("orderActivityCollection", []) or []:
        for leg in act.get("executionLegs", []) or []:
            p = leg.get("price")
            if p is not None:
                prices.append(p)
    return prices[-1] if prices else None


def _leg_fill_prices(o: dict[str, Any]) -> dict[Any, float]:
    """Volume-weighted fill price per order leg, keyed by legId, from the
    execution activity. Lets multi-leg orders (rolls/spreads) price each leg."""
    agg: dict[Any, list[float]] = {}
    for act in o.get("orderActivityCollection", []) or []:
        for el in act.get("executionLegs", []) or []:
            lid = el.get("legId")
            px = el.get("price")
            qty = el.get("quantity") or 0
            if lid is None or px is None:
                continue
            slot = agg.setdefault(lid, [0.0, 0.0])
            slot[0] += px * qty
            slot[1] += qty
    return {lid: (s[0] / s[1]) for lid, s in agg.items() if s[1]}


def parse_record(o: dict[str, Any], sc) -> dict[str, Any]:
    """Flatten a Schwab order into a storable record, with option legs decoded
    (underlying / put-call / strike / expiration) and per-leg fill prices for
    the closed-trade tabs."""
    leg_prices = _leg_fill_prices(o)
    legs_out = []
    for leg in o.get("orderLegCollection", []) or []:
        instr = leg.get("instrument", {}) or {}
        sym = instr.get("symbol", "")
        asset = (instr.get("assetType") or "").upper()
        lid = leg.get("legId")
        rl: dict[str, Any] = {
            "instruction": leg.get("instruction", ""),
            "positionEffect": leg.get("positionEffect", ""),
            "quantity": leg.get("quantity"),
            "assetType": asset,
            "symbol": sym,
            "legId": lid,
            "fillPrice": leg_prices.get(lid),
        }
        if asset == "OPTION":
            rl["ticker"] = sc._underlying_symbol(instr)
            rl["putCall"] = instr.get("putCall", "")
            rl["strike"] = sc._option_strike(sym)
            rl["expiration"] = sc._option_expiration(sym)
        else:
            rl["ticker"] = sym
        legs_out.append(rl)

    first = legs_out[0] if legs_out else {}
    return {
        "orderId": o.get("orderId"),
        "enteredTime": o.get("enteredTime", ""),
        "closeTime": o.get("closeTime", ""),
        "status": o.get("status", ""),
        "orderType": o.get("orderType", ""),
        "quantity": o.get("quantity"),
        "filledQuantity": o.get("filledQuantity"),
        "price": o.get("price"),
        "fillPrice": _fill_price(o),
        "symbol": first.get("ticker", ""),
        "instruction": first.get("instruction", ""),
        "legs": legs_out,
    }


def _orders_window(c, account_hash: str, from_dt: datetime, to_dt: datetime) -> list[dict[str, Any]]:
    last_exc: Exception | None = None
    for attempt in range(WINDOW_RETRIES + 1):
        try:
            resp = c.get_orders_for_account(
                account_hash, from_entered_datetime=from_dt, to_entered_datetime=to_dt
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:
            last_exc = exc
            if attempt < WINDOW_RETRIES:
                time.sleep(2)
    raise last_exc  # type: ignore[misc]


def backfill(c, sc, account_hash: str) -> tuple[list[dict[str, Any]], str | None]:
    now = datetime.now(timezone.utc)
    records: dict[Any, dict[str, Any]] = {}
    earliest: str | None = None
    empty = 0
    for w in range(BACKFILL_MAX_WINDOWS):
        to_dt = now - timedelta(days=BACKFILL_WINDOW_DAYS * w)
        from_dt = now - timedelta(days=BACKFILL_WINDOW_DAYS * (w + 1))
        try:
            orders = _orders_window(c, account_hash, from_dt, to_dt)
        except Exception as exc:
            print(f"  stopped at {from_dt.date()} (API limit/error: {exc})")
            break
        kept = 0
        for o in orders:
            if not _is_executed(o):
                continue
            kept += 1
            rec = parse_record(o, sc)
            if rec["orderId"] is not None:
                records[rec["orderId"]] = rec
            t = rec["enteredTime"]
            if t and (earliest is None or t < earliest):
                earliest = t
        print(f"  {from_dt.date()} → {to_dt.date()}: {len(orders)} orders ({kept} filled)")
        if not orders:
            empty += 1
            if empty >= STOP_AFTER_EMPTY:
                break
        else:
            empty = 0
    ordered = sorted(records.values(), key=lambda r: r["enteredTime"])
    return ordered, earliest


def rolling_update(c, sc, account_hash: str, existing: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    now = datetime.now(timezone.utc)
    orders = _orders_window(c, account_hash, now - timedelta(days=ROLLING_DAYS), now)
    by_id = {r["orderId"]: r for r in existing if r.get("orderId") is not None}
    for o in orders:
        if not _is_executed(o):
            continue
        rec = parse_record(o, sc)
        if rec["orderId"] is not None:
            by_id[rec["orderId"]] = rec  # upsert: overwrite the last 7 days
    ordered = sorted(by_id.values(), key=lambda r: r["enteredTime"])
    return ordered, len(orders)


def _txns_window(c, account_hash: str, from_dt: datetime, to_dt: datetime) -> list[dict[str, Any]]:
    """Pull TRADE + RECEIVE_AND_DELIVER transactions for a window (used for stock
    round-trips, including assignment cost basis)."""
    types = [
        c.Transactions.TransactionType.TRADE,
        c.Transactions.TransactionType.RECEIVE_AND_DELIVER,
    ]
    last_exc: Exception | None = None
    for attempt in range(WINDOW_RETRIES + 1):
        try:
            resp = c.get_transactions(
                account_hash, start_date=from_dt, end_date=to_dt, transaction_types=types
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:
            last_exc = exc
            if attempt < WINDOW_RETRIES:
                time.sleep(2)
    raise last_exc  # type: ignore[misc]


def sync_txns(c, account_hash: str, existing: list[dict[str, Any]], deep: bool = True) -> tuple[list[dict[str, Any]], int]:
    """Pull recent transactions and UPSERT by activityId into the persisted store.

    deep=True  → walk the full ~58-day lookback in windows (initial seed / --full),
                 so history accumulates beyond 60 days across runs.
    deep=False → pull only the last TXN_ROLLING_DAYS in one window (frequent runs),
                 keeping the every-minute pull small. Older history is retained
                 because we upsert into the existing store rather than replacing it.
    """
    now = datetime.now(timezone.utc)
    by_id = {t.get("activityId"): t for t in existing if t.get("activityId") is not None}
    pulled = 0

    if not deep:
        from_dt = now - timedelta(days=TXN_ROLLING_DAYS)
        try:
            txns = _txns_window(c, account_hash, from_dt, now)
        except Exception as exc:
            print(f"  txns {from_dt.date()} → {now.date()} skipped ({exc})")
            txns = []
        for t in txns:
            aid = t.get("activityId")
            if aid is not None:
                by_id[aid] = t
        pulled += len(txns)
        print(f"  txns {from_dt.date()} → {now.date()}: {len(txns)} (rolling {TXN_ROLLING_DAYS}d)")
        ordered = sorted(by_id.values(), key=lambda t: t.get("tradeDate") or t.get("time") or "")
        return ordered, pulled

    w = 0
    while True:
        to_dt = now - timedelta(days=BACKFILL_WINDOW_DAYS * w)
        from_dt = now - timedelta(days=min(BACKFILL_WINDOW_DAYS * (w + 1), TXN_LOOKBACK_DAYS))
        if from_dt >= to_dt:
            break
        try:
            txns = _txns_window(c, account_hash, from_dt, to_dt)
        except Exception as exc:
            print(f"  txns {from_dt.date()} → {to_dt.date()} skipped ({exc})")
            txns = []
        for t in txns:
            aid = t.get("activityId")
            if aid is not None:
                by_id[aid] = t
        pulled += len(txns)
        print(f"  txns {from_dt.date()} → {to_dt.date()}: {len(txns)}")
        w += 1
        if BACKFILL_WINDOW_DAYS * w >= TXN_LOOKBACK_DAYS:
            break
    ordered = sorted(by_id.values(), key=lambda t: t.get("tradeDate") or t.get("time") or "")
    return ordered, pulled


def main() -> None:
    load_dotenv()
    import schwab_client as sc

    data_dir = _data_dir()
    path = os.path.join(data_dir, HISTORY_FILE)
    force_full = "--full" in sys.argv

    store: dict[str, list[dict[str, Any]]] = {}
    if os.path.exists(path) and not force_full:
        try:
            with open(path, encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            store = {}

    txns_path = os.path.join(data_dir, TXNS_FILE)
    txns_store: dict[str, list[dict[str, Any]]] = {}
    if os.path.exists(txns_path):
        try:
            with open(txns_path, encoding="utf-8") as f:
                txns_store = json.load(f)
        except Exception:
            txns_store = {}

    c = sc.get_client()
    # The default 30s HTTP timeout is too short for busy order windows; raise it.
    try:
        c.set_timeout(REQUEST_TIMEOUT)
    except Exception:
        try:
            c.session.timeout = REQUEST_TIMEOUT
        except Exception:
            pass
    accounts = sc.list_accounts(c)
    if not accounts:
        raise SystemExit("No linked Schwab accounts found.")

    for acct in accounts:
        aid = acct["hash"]
        last4 = (acct.get("number") or "")[-4:]
        existing = store.get(aid, [])
        if force_full or aid not in store:
            print(f"Backfilling full history for ****{last4} ...")
            recs, earliest = backfill(c, sc, aid)
            print(f"  → {len(recs)} records; earliest entered {earliest or 'n/a'}")
        else:
            print(f"Refreshing last {ROLLING_DAYS} days for ****{last4} ...")
            recs, pulled = rolling_update(c, sc, aid, existing)
            print(f"  → pulled {pulled} orders; {len(recs)} records total after upsert")
        store[aid] = recs

        # Transactions feed (stocks incl. assignment cost basis). Deep ~58-day
        # pull on a full/first-time sync; otherwise a small rolling window so the
        # every-minute loop stays light. Upsert keeps older history either way.
        # Key off whether we've seen the account before (not whether its list is
        # empty) so a no-activity account doesn't re-backfill on every tick.
        existing_txns = txns_store.get(aid, [])
        deep_txns = force_full or aid not in txns_store
        print(f"  Syncing transactions for ****{last4} ({'deep' if deep_txns else 'rolling'}) ...")
        txns, tpulled = sync_txns(c, aid, existing_txns, deep=deep_txns)
        txns_store[aid] = txns
        print(f"  → pulled {tpulled} txns; {len(txns)} total after upsert")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(mask_pii(store), f, indent=2, ensure_ascii=False)
    print(f"Wrote {path}")

    with open(txns_path, "w", encoding="utf-8") as f:
        json.dump(mask_pii(txns_store), f, indent=2, ensure_ascii=False)
    print(f"Wrote {txns_path}")

    # Rebuild the app's closed-trade tabs from the updated history (pure Python).
    import closed_trades
    counts = closed_trades.write_closed(data_dir, store)
    print(
        f"Rebuilt closed tabs — CSPs: {counts['csp']}, LEAPs: {counts['leap']}, "
        f"spreads: {counts['spread']}, covered calls: {counts['covered']}, stocks: {counts['stock']}."
    )


if __name__ == "__main__":
    main()
