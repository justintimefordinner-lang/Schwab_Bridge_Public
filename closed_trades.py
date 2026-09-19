"""
closed_trades.py
================

Turns the executed-trade log (trade-history.json) into the two files the app's
closed-trade tabs read:

  • csp-closed.json    — short-put (cash-secured put) round-trips
  • leaps-closed.json  — long call / long put (LEAP) round-trips

For each option contract it FIFO-matches opening fills against closing fills,
producing one round-trip per matched quantity, and computes realized P&L.
Anything still open whose expiration has passed with no closing fill is treated
as expired (CSP = kept the credit; long LEAP = expired worthless).

Read-only. Pure-Python reconstruction (no Schwab calls), so sync_trade_history
calls it after each update, or run it standalone:
    python closed_trades.py
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import date, datetime, timezone
from typing import Any, Callable


def _no_fee(_order_id: Any) -> float:
    return 0.0

HISTORY_FILE = "trade-history.json"
CSP_FILE = "csp-closed.json"
LEAPS_FILE = "leaps-closed.json"
SPREADS_FILE = "spreads-closed.json"
COVERED_FILE = "covered-closed.json"
STOCKS_FILE = "stocks-closed.json"
TXNS_FILE = "transactions.json"
UNRESOLVED_FILE = "stocks-unresolved.json"     # bridge writes: stock sales needing a manual cost basis
MANUAL_BASIS_FILE = "manual_cost_basis.json"   # app writes: {orphanId: {"costPerShare": x}}
MANUAL_SALES_FILE = "manual_stock_sales.json"  # app writes: [{symbol, shares, proceedsPerShare, costPerShare, acquiredDate, soldDate}] — sales predating the feed entirely
SOURCE_LABEL = "schwab-bridge"

OPEN_INSTRUCTIONS = {"SELL_TO_OPEN", "BUY_TO_OPEN"}
CLOSE_INSTRUCTIONS = {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}
_EQUITY_ORDER_INSTRUCTIONS = {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}


# ---------------------------------------------------------------------------
# Order records synthesized from the TRANSACTIONS feed.
#
# The order store can miss fills: a backfill window that errored used to end the
# whole walk, and a multi-day sync gap can outrun the rolling window. Each fill
# still shows up as a TRADE transaction, which carries everything a round-trip
# needs — the OCC symbol, signed quantity, price, OPENING/CLOSING and the
# orderId. So any orderId present in the transactions but absent from the order
# store is rebuilt here in parse_record's shape and fed to the same FIFO. That
# is what turns a "put expired, kept the whole credit" into the buy-to-close it
# really was.
# ---------------------------------------------------------------------------
def _occ_parts(occ: str) -> tuple[float | None, str | None]:
    """(strike, expiration ISO) from an OCC symbol like 'SOFI  261009P00016000';
    (None, None) when it doesn't parse. Local copy of schwab_client's helpers so
    this module stays free of API imports."""
    import re
    m = re.search(r"(\d{6})([CP])(\d{8})$", (occ or "").replace(" ", ""))
    if not m:
        return None, None
    try:
        exp = datetime.strptime(m.group(1), "%y%m%d").date().isoformat()
    except ValueError:
        exp = None
    return int(m.group(3)) / 1000.0, exp


def _instruction_from_txn(position_effect: str, amount: float) -> str:
    pe = (position_effect or "").upper()
    if pe == "OPENING":
        return "BUY_TO_OPEN" if amount > 0 else "SELL_TO_OPEN"
    if pe == "CLOSING":
        return "BUY_TO_CLOSE" if amount > 0 else "SELL_TO_CLOSE"
    return "BUY_TO_OPEN" if amount > 0 else "SELL_TO_CLOSE"


def _orders_from_txns(txns: list[dict[str, Any]], known_order_ids: set[Any]) -> list[dict[str, Any]]:
    """Option orders present in the transactions feed but not in the order store,
    rebuilt as order records. Multi-fill orders (several TRADE records under one
    orderId) collapse into one record with volume-weighted leg prices."""
    by_order: dict[Any, dict[str, Any]] = {}
    for t in txns:
        if (t.get("type") or "").upper() != "TRADE":
            continue
        oid = t.get("orderId")
        if oid is None or oid in known_order_ids:
            continue
        when = t.get("time") or t.get("tradeDate") or ""
        for ti in t.get("transferItems", []) or []:
            instr = ti.get("instrument") or {}
            if (instr.get("assetType") or "").upper() != "OPTION":
                continue
            occ = instr.get("symbol")
            amount = ti.get("amount") or 0
            price = ti.get("price")
            if not occ or not amount or price is None:
                continue
            rec = by_order.setdefault(oid, {"orderId": oid, "enteredTime": when, "closeTime": when,
                                            "status": "FILLED", "orderType": "", "quantity": None,
                                            "filledQuantity": None, "price": None, "fillPrice": None,
                                            "symbol": instr.get("underlyingSymbol") or "", "instruction": "",
                                            "legs": [], "_legs": {}, "synthesized": True})
            if when and when > rec["closeTime"]:
                rec["closeTime"] = when
            if when and when < rec["enteredTime"]:
                rec["enteredTime"] = when
            instruction = _instruction_from_txn(ti.get("positionEffect") or "", amount)
            key = (occ, instruction)
            leg = rec["_legs"].get(key)
            if leg is None:
                occ_strike, occ_exp = _occ_parts(occ)
                strike = instr.get("strikePrice")
                if strike is None:
                    strike = occ_strike
                exp = (instr.get("expirationDate") or "")[:10] or occ_exp or ""
                leg = {
                    "instruction": instruction,
                    "positionEffect": (ti.get("positionEffect") or "").upper(),
                    "quantity": 0.0, "_notional": 0.0,
                    "assetType": "OPTION", "symbol": occ, "legId": len(rec["_legs"]) + 1,
                    "fillPrice": None,
                    "ticker": instr.get("underlyingSymbol") or "",
                    "putCall": (instr.get("putCall") or "").upper(),
                    "strike": float(strike) if strike is not None else None,
                    "expiration": exp or None,
                }
                rec["_legs"][key] = leg
            leg["quantity"] += abs(amount)
            leg["_notional"] += abs(amount) * float(price)
    out: list[dict[str, Any]] = []
    for rec in by_order.values():
        legs = list(rec.pop("_legs").values())
        for leg in legs:
            leg["fillPrice"] = leg["_notional"] / leg["quantity"] if leg["quantity"] else None
            del leg["_notional"]
        if not legs:
            continue
        rec["legs"] = legs
        rec["instruction"] = legs[0]["instruction"]
        rec["fillPrice"] = legs[0]["fillPrice"] if len(legs) == 1 else None
        out.append(rec)
    return out


def _equity_events_from_orders(orders: list[dict[str, Any]], skip_order_ids: set[Any]) -> dict[str, list[dict[str, Any]]]:
    """Stock buys/sells from the ORDER store, for orders the transactions feed
    doesn't cover (it reaches back ~60 days; orders go back much further). Only
    orders whose id is NOT in the transactions feed are used, so a fill is never
    counted from both sources. Assignments don't appear here — they have no order.
    Same event shape as _equity_events_from_txns."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for o in orders:
        oid = o.get("orderId")
        if oid is None or oid in skip_order_ids:
            continue
        legs = o.get("legs", []) or []
        for leg in legs:
            if (leg.get("assetType") or "").upper() not in _EQUITY_ASSET_TYPES:
                continue
            instruction = (leg.get("instruction") or "").upper()
            if instruction not in _EQUITY_ORDER_INSTRUCTIONS:
                continue
            sym = leg.get("symbol")
            qty = abs(leg.get("quantity") or 0)
            price = leg.get("fillPrice")
            if price is None and len(legs) == 1:
                price = o.get("fillPrice")
            if not sym or not qty or price is None:
                continue
            by_sym.setdefault(sym, []).append({
                "time": o.get("closeTime") or o.get("enteredTime") or "",
                "instruction": instruction, "qty": qty, "price": float(price), "order_id": oid,
            })
    return by_sym


def _data_dir() -> str:
    d = os.environ.get("APP_DATA_DIR")
    if not d or not os.path.isdir(d):
        raise SystemExit("Set APP_DATA_DIR in .env to the app's data folder.")
    return d


def _date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(iso[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _days_held(open_iso: str, close_iso: str) -> int:
    a, b = _date(open_iso), _date(close_iso)
    if a and b:
        return max(1, (b - a).days)
    return 1


def _events_for_contract(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group every option leg into per-contract (OCC symbol) event lists."""
    events: dict[str, list[dict[str, Any]]] = {}
    for o in records:
        opt_legs = [l for l in o.get("legs", []) if (l.get("assetType") or "").upper() == "OPTION"]
        order_fill = o.get("fillPrice")
        for leg in opt_legs:
            occ = leg.get("symbol")
            if not occ:
                continue
            price = leg.get("fillPrice")
            if price is None and len(opt_legs) == 1:
                price = order_fill  # single-leg order: leg price == order fill
            events.setdefault(occ, []).append({
                # Fill/terminal time (closeTime), NOT placement time (enteredTime):
                # a GTC limit placed weeks before it fills must be dated — and
                # FIFO-ordered — by when it actually executed, or the round-trip
                # lands on the submit date and shows the wrong close date / days-held.
                "time": o.get("closeTime") or o.get("enteredTime", "") or "",
                "instruction": (leg.get("instruction") or "").upper(),
                "positionEffect": (leg.get("positionEffect") or "").upper(),
                "qty": abs(leg.get("quantity") or 0),
                "price": price,
                "putCall": (leg.get("putCall") or "").upper(),
                "strike": leg.get("strike"),
                "expiration": leg.get("expiration"),
                "ticker": leg.get("ticker") or o.get("symbol") or "",
                "orderId": o.get("orderId"),
            })
    return events


def _is_open(ev: dict[str, Any]) -> bool:
    if ev["instruction"] in OPEN_INSTRUCTIONS:
        return True
    if ev["instruction"] in CLOSE_INSTRUCTIONS:
        return False
    return ev["positionEffect"] == "OPENING"


def _fifo_round_trips(evs: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Match opens to closes FIFO for one contract. Returns round-trips, each
    with open/close price, qty, side, and whether it closed by expiration."""
    evs = sorted(evs, key=lambda e: e["time"])
    open_lots: deque[dict[str, Any]] = deque()
    trips: list[dict[str, Any]] = []

    for ev in evs:
        if _is_open(ev):
            open_lots.append({
                "qty": ev["qty"], "price": ev["price"], "time": ev["time"],
                "short": ev["instruction"].startswith("SELL"),
                "putCall": ev["putCall"], "strike": ev["strike"],
                "expiration": ev["expiration"], "ticker": ev["ticker"],
                "orderId": ev.get("orderId"),
            })
        else:  # closing fill — consume open lots FIFO
            remaining = ev["qty"]
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                matched = min(remaining, lot["qty"])
                trips.append({
                    "qty": matched,
                    "open_time": lot["time"], "open_price": lot["price"],
                    "close_time": ev["time"], "close_price": ev["price"],
                    "short": lot["short"], "putCall": lot["putCall"],
                    "strike": lot["strike"], "expiration": lot["expiration"],
                    "ticker": lot["ticker"], "expired": False,
                    "open_order": lot.get("orderId"), "close_order": ev.get("orderId"),
                })
                lot["qty"] -= matched
                remaining -= matched
                if lot["qty"] == 0:
                    open_lots.popleft()

    # Leftover opens that have passed expiration with no close → expired.
    for lot in open_lots:
        exp = _date(lot["expiration"])
        if exp and exp < today:
            trips.append({
                "qty": lot["qty"], "open_time": lot["time"], "open_price": lot["price"],
                "close_time": lot["expiration"], "close_price": 0.0,
                "short": lot["short"], "putCall": lot["putCall"], "strike": lot["strike"],
                "expiration": lot["expiration"], "ticker": lot["ticker"], "expired": True,
                "open_order": lot.get("orderId"), "close_order": None,
            })
    return trips


def open_dates_by_occ(records: list[dict[str, Any]]) -> dict[str, str]:
    """For each option OCC symbol with lots still open after FIFO matching,
    return the open date (YYYY-MM-DD) of the earliest still-held lot. Used to
    stamp 'openedAt' onto live snapshot positions (Schwab's positions feed
    doesn't carry an open date)."""
    out: dict[str, str] = {}
    for occ, evs in _events_for_contract(records).items():
        open_lots: deque[dict[str, Any]] = deque()
        for ev in sorted(evs, key=lambda e: e["time"]):
            if _is_open(ev):
                open_lots.append({"qty": ev["qty"], "time": ev["time"]})
            else:  # closing fill consumes oldest open lots first
                remaining = ev["qty"]
                while remaining > 0 and open_lots:
                    lot = open_lots[0]
                    matched = min(remaining, lot["qty"])
                    lot["qty"] -= matched
                    remaining -= matched
                    if lot["qty"] == 0:
                        open_lots.popleft()
        if open_lots:
            earliest = min((lot["time"] for lot in open_lots if lot["time"]), default="")
            if earliest:
                out[occ] = earliest[:10]
    return out


def _round(n: float, d: int = 2) -> float:
    return round(n, d)


def _build_csp(t: dict[str, Any], fpc: Callable[[Any], float] = _no_fee, assigned: bool = False) -> dict[str, Any] | None:
    credit = t["open_price"]
    close_px = t["close_price"]
    strike = t["strike"]
    if credit is None or strike is None:
        return None  # can't price this round-trip; skip rather than guess
    qty = t["qty"]
    credit_received = credit * 100 * qty
    cost_to_close = (close_px or 0.0) * 100 * qty
    fees = (fpc(t.get("open_order")) + fpc(t.get("close_order"))) * qty
    realized = credit_received - cost_to_close - fees
    collateral = strike * 100 * qty
    days = _days_held(t["open_time"], t["close_time"])
    roc = realized / collateral if collateral else 0.0
    if assigned:
        # Assigned into shares: the net premium is folded into the assigned shares'
        # cost basis (see build_from_history), so it is NOT also booked as a realized
        # option gain here — otherwise it double-counts against the reduced stock avg.
        outcome = "assigned"
        realized = 0.0
        roc = 0.0
    else:
        outcome = "expired" if t["expired"] else ("closed_profit" if realized >= 0 else "closed_loss")
    return {
        "id": f"{t['ticker']}-{strike}P-{t['expiration']}-{t['open_time'][:10]}",
        "symbol": t["ticker"], "name": t["ticker"],
        "strike": strike, "expiration": t["expiration"] or "",
        "openedAt": t["open_time"][:10], "closedAt": (t["close_time"] or "")[:10],
        "contracts": qty,
        "creditPerShare": _round(credit), "creditReceived": _round(credit_received),
        "costToClose": _round(cost_to_close), "fees": _round(fees), "realizedPnl": _round(realized),
        "outcome": outcome, "daysHeld": days, "collateral": _round(collateral),
        "returnOnCollateral": _round(roc, 4), "annualized": _round(roc * 365 / days, 4) if days else 0.0,
    }


def _build_leap(t: dict[str, Any], fpc: Callable[[Any], float] = _no_fee) -> dict[str, Any] | None:
    entry = t["open_price"]
    close_px = t["close_price"]
    strike = t["strike"]
    if entry is None or strike is None:
        return None
    qty = t["qty"]
    cost_basis = entry * 100 * qty
    proceeds = (close_px or 0.0) * 100 * qty
    fees = (fpc(t.get("open_order")) + fpc(t.get("close_order"))) * qty
    realized = proceeds - cost_basis - fees
    days = _days_held(t["open_time"], t["close_time"])
    ret = realized / cost_basis if cost_basis else 0.0
    outcome = "expired" if t["expired"] else ("closed_profit" if realized >= 0 else "closed_loss")
    opt_type = "put" if t["putCall"] in ("PUT", "P") else "call"
    return {
        "id": f"{t['ticker']}-{strike}{opt_type[0].upper()}-{t['expiration']}-{t['open_time'][:10]}",
        "symbol": t["ticker"], "name": t["ticker"], "optionType": opt_type,
        "strike": strike, "expiration": t["expiration"] or "",
        "openedAt": t["open_time"][:10], "closedAt": (t["close_time"] or "")[:10],
        "contracts": qty,
        "entryPerShare": _round(entry), "costBasis": _round(cost_basis),
        "proceeds": _round(proceeds), "fees": _round(fees), "realizedPnl": _round(realized),
        "outcome": outcome, "daysHeld": days,
        "returnPct": _round(ret, 4), "annualized": _round(ret * 365 / days, 4),
    }


def _spread_partners(records: list[dict[str, Any]]) -> tuple[set[str], set[tuple[str, str]]]:
    """Identify vertical-spread legs from multi-leg orders, mirroring the Sheets
    logic: two legs of the SAME underlying + type + expiration at DIFFERENT
    strikes, one short-side and one long-side, form a vertical. Returns the set
    of spread-leg OCC symbols and the (short_occ, long_occ) pairs.

    A single-leg sell is a CSP, not a spread. A roll (two same-side legs at
    DIFFERENT expirations) is not a spread either — the expiration match guards
    against that.
    """
    short_side = {"SELL_TO_OPEN", "BUY_TO_CLOSE"}
    long_side = {"BUY_TO_OPEN", "SELL_TO_CLOSE"}
    spread_occs: set[str] = set()
    pairs: set[tuple[str, str]] = set()

    for o in records:
        opt = [l for l in o.get("legs", []) if (l.get("assetType") or "").upper() == "OPTION"]
        for i in range(len(opt)):
            for j in range(i + 1, len(opt)):
                a, b = opt[i], opt[j]
                if (
                    a.get("ticker") == b.get("ticker")
                    and (a.get("putCall") or "").upper() == (b.get("putCall") or "").upper()
                    and a.get("expiration") == b.get("expiration")
                    and a.get("strike") != b.get("strike")
                    and a.get("symbol") and b.get("symbol")
                    # Both legs must open together (OPENING) or close together (CLOSING)
                    # to be a vertical. A roll or diagonal — one leg CLOSING and one
                    # OPENING in the same order — is NOT a spread, even though it also
                    # has one short-side and one long-side leg.
                    and (a.get("positionEffect") or "").upper() == (b.get("positionEffect") or "").upper()
                    and (a.get("positionEffect") or "").upper() in ("OPENING", "CLOSING")
                ):
                    short_occ = long_occ = None
                    for leg in (a, b):
                        instr = (leg.get("instruction") or "").upper()
                        if instr in short_side:
                            short_occ = leg.get("symbol")
                        elif instr in long_side:
                            long_occ = leg.get("symbol")
                    if short_occ and long_occ:
                        spread_occs.add(short_occ)
                        spread_occs.add(long_occ)
                        pairs.add((short_occ, long_occ))
    return spread_occs, pairs


def _build_covered_call(t: dict[str, Any], fpc: Callable[[Any], float] = _no_fee, assigned: bool = False) -> dict[str, Any] | None:
    credit = t["open_price"]
    close_px = t["close_price"]
    strike = t["strike"]
    if credit is None or strike is None:
        return None
    qty = t["qty"]
    credit_received = credit * 100 * qty
    cost_to_close = (close_px or 0.0) * 100 * qty
    fees = (fpc(t.get("open_order")) + fpc(t.get("close_order"))) * qty
    realized = credit_received - cost_to_close - fees
    notional = strike * 100 * qty
    days = _days_held(t["open_time"], t["close_time"])
    ret = realized / notional if notional else 0.0
    if assigned:
        # Called away: the premium is added to the shares' sale proceeds (see
        # build_from_history), the way Schwab reports it — the call itself books
        # no realized gain, otherwise the premium counts twice.
        outcome = "assigned"
        realized = 0.0
        ret = 0.0
    else:
        outcome = "expired" if t["expired"] else ("closed_profit" if realized >= 0 else "closed_loss")
    return {
        "id": f"{t['ticker']}-{strike}C-{t['expiration']}-{t['open_time'][:10]}",
        "symbol": t["ticker"], "name": t["ticker"],
        "strike": strike, "expiration": t["expiration"] or "",
        "openedAt": t["open_time"][:10], "closedAt": (t["close_time"] or "")[:10],
        "contracts": qty,
        "creditPerShare": _round(credit), "creditReceived": _round(credit_received),
        "costToClose": _round(cost_to_close), "fees": _round(fees), "realizedPnl": _round(realized),
        "outcome": outcome, "daysHeld": days,
        "returnOnNotional": _round(ret, 4), "annualized": _round(ret * 365 / days, 4),
    }


def _build_spread(s: dict[str, Any], l: dict[str, Any], fpc: Callable[[Any], float] = _no_fee) -> dict[str, Any] | None:
    s_open, s_close = s["open_price"], s["close_price"]
    l_open, l_close = l["open_price"], l["close_price"]
    if s_open is None or l_open is None or s["strike"] is None or l["strike"] is None:
        return None
    qty = min(s["qty"], l["qty"])
    if qty <= 0:
        return None
    short_pnl = (s_open - (s_close or 0.0)) * 100 * qty      # credit − cost to close
    long_pnl = ((l_close or 0.0) - l_open) * 100 * qty        # proceeds − cost
    # Fees for all four legs (short open/close + long open/close), per contract.
    fees = (fpc(s.get("open_order")) + fpc(s.get("close_order"))
            + fpc(l.get("open_order")) + fpc(l.get("close_order"))) * qty
    realized = short_pnl + long_pnl - fees
    net_credit = s_open - l_open                              # per share, signed
    width = abs(s["strike"] - l["strike"])
    is_credit = net_credit >= 0
    max_risk = (max(width - net_credit, 0) if is_credit else (l_open - s_open)) * 100 * qty
    expired = s["expired"] and l["expired"]
    outcome = "expired" if expired else ("closed_profit" if realized >= 0 else "closed_loss")
    days = _days_held(s["open_time"], s["close_time"])
    ret = realized / max_risk if max_risk else 0.0
    opt = "put" if s["putCall"] in ("PUT", "P") else "call"
    return {
        "id": f"{s['ticker']}-{s['strike']}/{l['strike']}{opt[0].upper()}-{s['expiration']}-{s['open_time'][:10]}",
        "symbol": s["ticker"], "name": s["ticker"], "optionType": opt,
        "shortStrike": s["strike"], "longStrike": l["strike"], "width": _round(width),
        "expiration": s["expiration"] or "",
        "openedAt": s["open_time"][:10], "closedAt": (s["close_time"] or "")[:10],
        "contracts": qty, "isCredit": is_credit,
        "netCreditPerShare": _round(net_credit),
        "netOpen": _round(net_credit * 100 * qty),
        "netClose": _round(((s_close or 0.0) - (l_close or 0.0)) * 100 * qty),
        "fees": _round(fees), "realizedPnl": _round(realized), "maxRisk": _round(max_risk),
        "outcome": outcome, "daysHeld": days,
        "returnOnRisk": _round(ret, 4), "annualized": _round(ret * 365 / days, 4),
    }


def _closed_together(s: dict[str, Any], l: dict[str, Any]) -> bool:
    """Two legs form one spread only if they were closed as a unit — in the same
    closing order, or both expired. Legs closed on different dates ('legged out')
    are reported individually on their own close dates, matching how Schwab books
    each lot separately."""
    so, lo = s.get("close_order"), l.get("close_order")
    if so is not None and lo is not None:
        return so == lo
    if so is None and lo is None:
        return bool(s.get("expired") and l.get("expired"))
    return False


def _combine_spreads(s_trips: list[dict[str, Any]], l_trips: list[dict[str, Any]], fpc: Callable[[Any], float] = _no_fee) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair each short-leg round-trip with the long-leg round-trip it was CLOSED
    WITH, and value the pair as one spread. Returns (spread_records, leftovers):
    legs that were legged out (closed at different times) come back as leftovers
    so the caller reports them as individual single-leg trades on their own dates."""
    out: list[dict[str, Any]] = []
    longs = sorted(l_trips, key=lambda t: t["open_time"])
    used = [False] * len(longs)
    leftovers: list[dict[str, Any]] = []
    for s in sorted(s_trips, key=lambda t: t["open_time"]):
        mi = next((i for i, l in enumerate(longs) if not used[i] and _closed_together(s, l)), None)
        rec = _build_spread(s, longs[mi], fpc) if mi is not None else None
        if rec is not None:
            used[mi] = True
            out.append(rec)
        else:
            leftovers.append(s)  # no closed-together partner (or unvaluable) → individual
    leftovers.extend(l for i, l in enumerate(longs) if not used[i])
    return out, leftovers


_EQUITY_ASSET_TYPES = {"EQUITY", "COLLECTIVE_INVESTMENT"}


def _is_assignment(txn: dict[str, Any]) -> bool:
    """A RECEIVE_AND_DELIVER removes an option either by ASSIGNMENT (shares move
    at the strike) or by EXPIRATION (the option simply drops off — no shares
    move). Schwab distinguishes them only in the free-text description:
    'Removed due to Assignment ...' vs 'Removed due to Expiration ...'. Treating
    an expiration as an assignment invents a phantom stock disposal, so gate on
    this."""
    return "assignment" in (txn.get("description") or "").lower()


def _equity_events_from_txns(records: list[dict[str, Any]], prem_per_share: dict[tuple[str, float, str], float] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol chronological equity events from the TRANSACTIONS feed.

    Two sources, so assignment cost basis is captured (orders miss it):
      • TRADE with an EQUITY/ETF item → a real buy/sell. positionEffect + the
        sign of `amount` give open/close and long/short. Schwab posts the shares
        an assignment moves as one of these too: a TRADE with NO orderId at
        exactly the strike (verified against a Schwab lot report). That is the
        authoritative record of the assignment — real date, real share count.
      • RECEIVE_AND_DELIVER with an OPTION item → the assignment's option-removal
        side. It used to ALSO become a share event, which double-booked every
        assignment (500 shares bought at the netted price AND 500 at the raw
        strike), so FIFO later sold against phantom raw-strike lots and showed
        losses Schwab never had. Now it only supplies what the TRADE lacks —
        which strike's premium to net off the shares — and creates a share
        event itself only when no matching no-orderId TRADE exists (older feeds).
    """
    by_sym: dict[str, list[dict[str, Any]]] = {}
    # Assignment share movements from the TRADE side: (symbol, strike, qty) → events,
    # so the RECEIVE_AND_DELIVER pass below can claim (and price) them.
    strike_trades: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for t in records:
        ttype = (t.get("type") or "").upper()
        when = t.get("tradeDate") or t.get("time") or ""
        oid = t.get("orderId")
        for ti in t.get("transferItems", []) or []:
            instr = ti.get("instrument") or {}
            atype = (instr.get("assetType") or "").upper()

            if ttype == "TRADE" and atype in _EQUITY_ASSET_TYPES:
                amount = ti.get("amount") or 0
                price = ti.get("price")
                sym = instr.get("symbol")
                if not sym or not amount or price is None:
                    continue
                pe = (ti.get("positionEffect") or "").upper()
                if pe == "OPENING":
                    instruction = "BUY" if amount > 0 else "SELL_SHORT"
                elif pe == "CLOSING":
                    instruction = "SELL" if amount < 0 else "BUY_TO_COVER"
                else:  # fall back to sign
                    instruction = "BUY" if amount > 0 else "SELL"
                ev = {"time": when, "instruction": instruction, "qty": abs(amount), "price": float(price), "order_id": oid}
                by_sym.setdefault(sym, []).append(ev)
                if oid is None:
                    strike_trades.setdefault((sym, round(float(price), 2)), []).append(ev)

    for t in records:
        if (t.get("type") or "").upper() != "RECEIVE_AND_DELIVER":
            continue
        if not _is_assignment(t):
            continue   # expiration removal — the option is gone but no shares moved
        when = t.get("tradeDate") or t.get("time") or ""
        oid = t.get("orderId")
        for ti in t.get("transferItems", []) or []:
            instr = ti.get("instrument") or {}
            if (instr.get("assetType") or "").upper() != "OPTION":
                continue
            pc = (instr.get("putCall") or "").upper()
            strike = instr.get("strikePrice")
            underlying = instr.get("underlyingSymbol")
            contracts = abs(ti.get("amount") or 0)
            deliverables = instr.get("optionDeliverables") or []
            per = (deliverables[0].get("deliverableUnits") if deliverables else None) \
                or instr.get("optionPremiumMultiplier") or 100
            shares = contracts * per
            if not underlying or strike is None or shares <= 0 or pc not in ("PUT", "CALL"):
                continue
            skey = (underlying, round(float(strike), 2))
            prem = (prem_per_share or {}).get((underlying, round(float(strike), 2), pc), 0.0)
            # Option A: the matched option's premium lives in the shares rather than
            # being double-counted as a realized option gain — a put's premium comes
            # OFF the cost basis, a called-away call's premium goes ON the sale
            # proceeds. prem is 0.0 when no assigned option was matched.
            netted = float(strike) - prem if pc == "PUT" else float(strike) + prem
            # Claim the TRADE-side share movement(s) for this assignment: the same
            # symbol at exactly the strike, no orderId, until the contracts' shares
            # are covered. Price those events at the netted figure; add nothing.
            remaining = shares
            for ev in strike_trades.get(skey, []):
                if remaining <= 0:
                    break
                if ev.get("_claimed"):
                    continue
                want = "BUY" if pc == "PUT" else "SELL"
                if ev["instruction"] != want:
                    continue
                ev["_claimed"] = True
                ev["price"] = netted
                remaining -= ev["qty"]
            if remaining > 0:
                # No TRADE-side record (older feeds) — fall back to the old behaviour.
                by_sym.setdefault(underlying, []).append(
                    {"time": when, "instruction": "BUY" if pc == "PUT" else "SELL",
                     "qty": remaining, "price": netted, "order_id": oid}
                )
    for evs in by_sym.values():
        for ev in evs:
            ev.pop("_claimed", None)
    # Collapse multi-fill orders: one sell/buy order can fill in several lots, each
    # arriving as its own transaction under a shared orderId. Merge them so a
    # 1,000-share order that filled 934 + 66 is one 1,000-share event, not two.
    return {sym: _coalesce_orders(evs) for sym, evs in by_sym.items()}


def _coalesce_orders(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge executions that share an orderId + instruction into a single event:
    quantities summed, price share-weighted, earliest fill time kept. Events with
    no orderId (e.g. assignments) pass through untouched, so distinct dispositions
    stay separate."""
    merged: dict[tuple[Any, str], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for ev in events:
        oid = ev.get("order_id")
        if oid is None:
            out.append(ev)
            continue
        key = (oid, ev["instruction"])
        slot = merged.get(key)
        if slot is None:
            slot = {"time": ev["time"], "instruction": ev["instruction"],
                    "qty": 0.0, "_notional": 0.0, "order_id": oid}
            merged[key] = slot
            out.append(slot)
        slot["qty"] += ev["qty"]
        slot["_notional"] += ev["qty"] * ev["price"]
        if ev["time"] < slot["time"]:
            slot["time"] = ev["time"]
    for slot in merged.values():
        slot["price"] = slot["_notional"] / slot["qty"] if slot["qty"] else 0.0
        del slot["_notional"]
    return out


def _equity_round_trips(evs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FIFO-match equity opens to closes. Longs: BUY opens, SELL closes. Shorts:
    SELL_SHORT opens, BUY_TO_COVER closes. Long and short lots queue separately.
    Returns (round_trips, orphans): orphans are closes with no matching open lot
    (the purchase predates our transaction history) — they need a manual cost basis."""
    evs = sorted(evs, key=lambda e: e["time"])
    long_lots: deque[dict[str, Any]] = deque()
    short_lots: deque[dict[str, Any]] = deque()
    trips: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []

    def close_against(lots: deque[dict[str, Any]], ev: dict[str, Any], side: str) -> None:
        remaining = ev["qty"]
        while remaining > 0 and lots:
            lot = lots[0]
            matched = min(remaining, lot["qty"])
            trips.append({
                "side": side, "qty": matched,
                "open_time": lot["time"], "open_price": lot["price"],
                "close_time": ev["time"], "close_price": ev["price"],
            })
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] == 0:
                lots.popleft()
        if remaining > 0:
            # Sold more than we have an opening record for — the purchase is older
            # than the transaction feed. Surface it so the user can supply a basis.
            orphans.append({"side": side, "qty": remaining,
                            "close_time": ev["time"], "close_price": ev["price"],
                            "order_id": ev.get("order_id")})

    for ev in evs:
        instr = ev["instruction"]
        if instr == "BUY":
            long_lots.append({"qty": ev["qty"], "price": ev["price"], "time": ev["time"]})
        elif instr == "SELL_SHORT":
            short_lots.append({"qty": ev["qty"], "price": ev["price"], "time": ev["time"]})
        elif instr == "SELL":
            close_against(long_lots, ev, "long")
        elif instr == "BUY_TO_COVER":
            close_against(short_lots, ev, "short")
    return trips, orphans


def _build_stock(t: dict[str, Any], symbol: str, idx: int) -> dict[str, Any] | None:
    open_px, close_px = t["open_price"], t["close_price"]
    qty = t["qty"]
    if open_px is None or close_px is None or qty <= 0:
        return None
    cost_basis = open_px * qty            # capital at the opening side
    proceeds = close_px * qty
    # Long: gain when close > open. Short: gain when open(sell) > close(cover).
    realized = (proceeds - cost_basis) if t["side"] == "long" else (cost_basis - proceeds)
    days = _days_held(t["open_time"], t["close_time"])
    ret = realized / cost_basis if cost_basis else 0.0
    outcome = "closed_profit" if realized >= 0 else "closed_loss"
    return {
        "id": f"{symbol}-{t['open_time'][:10]}-{t['close_time'][:10]}-{idx}",
        "symbol": symbol, "name": symbol, "side": t["side"], "shares": _round(qty, 4),
        "avgOpen": _round(open_px, 4), "avgClose": _round(close_px, 4),
        "costBasis": _round(cost_basis), "proceeds": _round(proceeds),
        "realizedPnl": _round(realized), "outcome": outcome,
        "openedAt": t["open_time"][:10], "closedAt": (t["close_time"] or "")[:10],
        "daysHeld": days, "returnPct": _round(ret, 4), "annualized": _round(ret * 365 / days, 4) if days else 0.0,
    }


_FEE_TYPES = {"COMMISSION", "OPT_REG_FEE", "SEC_FEE", "TAF_FEE", "INDEX_OPTION_FEE"}


def _fee_index_by_order(txns_store: dict[str, list[dict[str, Any]]] | None) -> dict[Any, dict[str, float]]:
    """Total option fees and option-contract count per orderId, from the
    transactions feed. Fees live in transferItems whose `feeType` is set, with
    the dollar amount in `cost` (negative = charged). Only TRADE transactions
    that carry an OPTION leg are counted, so fees attach to option orders. A
    partially-filled order spans several transactions sharing one orderId, so we
    accumulate. Returns {orderId: {"fee": dollars, "contracts": n}}."""
    idx: dict[Any, dict[str, float]] = {}
    for records in (txns_store or {}).values():
        for t in records:
            if (t.get("type") or "").upper() != "TRADE":
                continue
            oid = t.get("orderId")
            if oid is None:
                continue
            fee = 0.0
            contracts = 0.0
            has_option = False
            for ti in t.get("transferItems", []) or []:
                ft = (ti.get("feeType") or "").upper()
                if ft:
                    if ft in _FEE_TYPES:
                        c = ti.get("cost")
                        if isinstance(c, (int, float)):
                            fee += abs(c)
                    continue
                instr = ti.get("instrument") or {}
                if (instr.get("assetType") or "").upper() == "OPTION":
                    has_option = True
                    contracts += abs(ti.get("amount") or 0)
            if not has_option:
                continue
            slot = idx.setdefault(oid, {"fee": 0.0, "contracts": 0.0})
            slot["fee"] += fee
            slot["contracts"] += contracts
    return idx


def _assignment_contracts(records: list[dict[str, Any]]) -> dict[tuple[str, float, str], float]:
    """From one account's TRANSACTIONS feed: total assigned contracts per
    (underlying, strike, 'PUT'|'CALL'). Lets us tell which expired short options
    were actually assigned (vs expired worthless): an assigned put's premium is
    folded into the shares' cost basis, an assigned (called-away) call's premium
    into the shares' sale proceeds — Schwab's convention — instead of being
    double-counted as a realized option gain."""
    out: dict[tuple[str, float, str], float] = {}
    for t in records:
        if (t.get("type") or "").upper() != "RECEIVE_AND_DELIVER":
            continue
        if not _is_assignment(t):
            continue   # expired, not assigned — no shares, no premium to fold
        for ti in t.get("transferItems", []) or []:
            instr = ti.get("instrument") or {}
            if (instr.get("assetType") or "").upper() != "OPTION":
                continue
            pc = (instr.get("putCall") or "").upper()
            if pc not in ("PUT", "CALL"):
                continue
            strike = instr.get("strikePrice")
            underlying = instr.get("underlyingSymbol")
            contracts = abs(ti.get("amount") or 0)
            if underlying and strike is not None and contracts > 0:
                key = (underlying, round(float(strike), 2), pc)
                out[key] = out.get(key, 0.0) + contracts
    return out


def _orphan_id(symbol: str, orph: dict[str, Any]) -> str:
    """Stable id for an unmatched stock sale, so a user-entered basis sticks
    across rebuilds. Two same-day sales at the same price (e.g. a market sell and
    a covered-call assignment) are disambiguated by the '#N' counter in
    build_from_history rather than the orderId, so ids stay stable even for
    assignment legs that carry no orderId."""
    return f"{symbol}|{(orph.get('close_time') or '')[:10]}|{orph['qty']:g}|{orph.get('close_price')}"


def build_from_history(
    store: dict[str, list[dict[str, Any]]],
    txns_store: dict[str, list[dict[str, Any]]] | None = None,
    manual_basis: dict[str, dict[str, Any]] | None = None,
    manual_sales: list[dict[str, Any]] | None = None,
) -> dict[str, list]:
    today = datetime.now(timezone.utc).date()
    csp_closed: list[dict[str, Any]] = []
    leap_closed: list[dict[str, Any]] = []
    spread_closed: list[dict[str, Any]] = []
    covered_closed: list[dict[str, Any]] = []
    stock_closed: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []   # stock sales whose purchase predates our data
    seen_orphan_ids: dict[str, int] = {}    # guarantees each surfaced sale has a unique id
    manual_basis = manual_basis or {}

    # Per-contract option fees, joined to round-trips by orderId, so realized P&L
    # comes out net of commissions/regulatory fees (matching Schwab).
    fee_idx = _fee_index_by_order(txns_store)

    def fpc(order_id: Any) -> float:
        """Per-contract fee for an order. 0 when the order isn't in the ~58-day
        transaction window (e.g. a leg opened before the feed began), so those
        legs stay un-netted rather than guessed."""
        slot = fee_idx.get(order_id)
        if not slot or not slot["contracts"]:
            return 0.0
        return slot["fee"] / slot["contracts"]

    txns_store = txns_store or {}
    # Process each account with BOTH feeds together, so an assigned put (orders
    # feed) can hand its premium to its shares (transactions feed) within the
    # same account.
    for aid in set(store) | set(txns_store):
        orders = list(store.get(aid, []))
        txns = txns_store.get(aid, [])
        # Fills the order sync missed but the transactions feed has: rebuild them
        # as orders so their closes count (see _orders_from_txns).
        known_ids = {o.get("orderId") for o in orders if o.get("orderId") is not None}
        recovered = _orders_from_txns(txns, known_ids)
        if recovered:
            print(f"  recovered {len(recovered)} option orders from the transactions feed")
            orders.extend(recovered)
        txn_order_ids = {t.get("orderId") for t in txns if t.get("orderId") is not None}
        # Where each output list stood before this account, so everything it adds
        # can be stamped with the account at the end of the loop.
        _outputs = (csp_closed, leap_closed, spread_closed, covered_closed, stock_closed, unresolved)
        _marks = [len(lst) for lst in _outputs]

        # PUT contracts assigned per (underlying, strike); we draw from this budget
        # to decide which expired short puts were assigned.
        assign_budget = _assignment_contracts(txns)
        # Net premium of the options we mark assigned, to fold into those shares'
        # basis (puts) or proceeds (calls): {(underlying, strike, pc): [net_premium_dollars, shares]}.
        assign_premium: dict[tuple[str, float, str], list[float]] = {}

        # ---- Options: from the ORDERS feed ----
        spread_occs, pairs = _spread_partners(orders)
        trips_by_occ = {occ: _fifo_round_trips(evs, today) for occ, evs in _events_for_contract(orders).items()}

        # Spreads: merge only legs CLOSED TOGETHER; legged-out legs come back as
        # leftovers to be reported individually on their own close dates.
        spread_leftovers: list[dict[str, Any]] = []
        for short_occ, long_occ in pairs:
            recs, leftover = _combine_spreads(trips_by_occ.get(short_occ, []), trips_by_occ.get(long_occ, []), fpc)
            spread_closed.extend(recs)
            spread_leftovers.extend(leftover)

        # Individual single-leg trades: every non-spread contract's round-trips,
        # plus any legged-out spread legs.
        individual = [t for occ, trips in trips_by_occ.items() if occ not in spread_occs for t in trips]
        individual.extend(spread_leftovers)

        for t in individual:
            is_put = t["putCall"] in ("PUT", "P")
            if t["short"] and is_put:
                # A short put that passed expiration with no closing order is either
                # expired-worthless OR assigned. If the transactions feed shows an
                # assignment at this (underlying, strike), treat it as assigned and
                # hand its premium to the shares (Option A).
                key = (t["ticker"], round(float(t["strike"]), 2), "PUT") if t["strike"] is not None else None
                assigned = bool(t["expired"] and key and assign_budget.get(key, 0.0) >= t["qty"])
                rec = _build_csp(t, fpc, assigned=assigned)
                if not rec:
                    continue
                csp_closed.append(rec)
                if assigned and key:
                    assign_budget[key] -= t["qty"]
                    slot = assign_premium.setdefault(key, [0.0, 0.0])
                    slot[0] += rec["creditReceived"] - rec["fees"]  # net premium $
                    slot[1] += t["qty"] * 100                       # shares assigned
            elif t["short"] and not is_put:
                # A short call that passed expiration with no closing order was either
                # expired-worthless or called away. Same test as the puts above.
                key = (t["ticker"], round(float(t["strike"]), 2), "CALL") if t["strike"] is not None else None
                assigned = bool(t["expired"] and key and assign_budget.get(key, 0.0) >= t["qty"])
                rec = _build_covered_call(t, fpc, assigned=assigned)    # short call = covered call
                if not rec:
                    continue
                covered_closed.append(rec)
                if assigned and key:
                    assign_budget[key] -= t["qty"]
                    slot = assign_premium.setdefault(key, [0.0, 0.0])
                    slot[0] += rec["creditReceived"] - rec["fees"]  # net premium $
                    slot[1] += t["qty"] * 100                       # shares called away
            elif not t["short"]:
                rec = _build_leap(t, fpc)            # long call/put = LEAP
                if rec:
                    leap_closed.append(rec)

        # ---- Stocks: from the TRANSACTIONS feed, with assigned-put premium netted
        #      off the shares' cost basis (contracts-weighted per strike). Sales whose
        #      purchase predates the feed get a user-entered basis if one is on file,
        #      otherwise they're surfaced as "unresolved" for the app to prompt. ----
        prem_per_share = {k: (v[0] / v[1] if v[1] else 0.0) for k, v in assign_premium.items()}
        equity_events = _equity_events_from_txns(txns, prem_per_share)
        # Stock fills older than the transactions feed reaches come from the order
        # store instead (orders whose id the feed doesn't carry). Both sources feed
        # one FIFO per symbol, so a January buy can match an August sale.
        for sym, evs in _equity_events_from_orders(orders, txn_order_ids).items():
            equity_events.setdefault(sym, []).extend(evs)
        for sym, evs in equity_events.items():
            rts, orphans = _equity_round_trips(evs)
            for i, t in enumerate(rts):
                rec = _build_stock(t, sym, i)
                if rec:
                    stock_closed.append(rec)
            for orph in orphans:
                oid = _orphan_id(sym, orph)
                n = seen_orphan_ids.get(oid, 0)
                seen_orphan_ids[oid] = n + 1
                if n:
                    oid = f"{oid}#{n}"   # identical unmatched sale — keep ids distinct
                entry = manual_basis.get(oid) or {}
                cps = entry.get("costPerShare")
                acq = entry.get("acquiredDate")
                if cps is not None:
                    # acquiredDate (when supplied) becomes the real open date, so
                    # daysHeld and the short/long-term bucket come out correct;
                    # without it the holding period is unknown → defaults short.
                    t = {"side": orph["side"], "qty": orph["qty"],
                         "open_price": float(cps), "close_price": orph["close_price"],
                         "open_time": acq or "", "close_time": orph["close_time"]}
                    rec = _build_stock(t, sym, oid)
                    if rec:
                        rec["manualBasis"] = True
                        stock_closed.append(rec)
                # Surface for completion when it still lacks a cost basis OR an
                # acquired date (needed to classify short- vs long-term). Carry the
                # known cost so the app pre-fills it.
                if cps is None or not acq:
                    unresolved.append({
                        "id": oid, "symbol": sym, "side": orph["side"],
                        "shares": _round(orph["qty"], 4),
                        "soldAt": _round(orph.get("close_price") or 0.0, 4),
                        "closeDate": (orph.get("close_time") or "")[:10],
                        "costPerShare": _round(float(cps), 4) if cps is not None else None,
                        "acquiredDate": acq or None,
                    })

        # Stamp everything this account produced with its id — the same opaque hash
        # the snapshot uses for the account — so the app can reconcile realized P&L
        # against a broker report one account at a time. Schwab's reports are per
        # account; an unstamped record can only be compared in aggregate.
        for lst, start in zip(_outputs, _marks):
            for rec in lst[start:]:
                rec["accountId"] = aid

    # Fully user-added stock sales (predate the feed entirely, so they never show up
    # as orphans). Each is a closed long round-trip with a real acquired/sold date.
    for i, s in enumerate(manual_sales or []):
        try:
            t = {"side": "long", "qty": float(s["shares"]),
                 "open_price": float(s["costPerShare"]), "close_price": float(s["proceedsPerShare"]),
                 "open_time": s.get("acquiredDate") or "", "close_time": s.get("soldDate") or ""}
        except (KeyError, TypeError, ValueError):
            continue
        rec = _build_stock(t, str(s.get("symbol", "")).upper(), f"manual-{i}")
        if rec:
            rec["manualBasis"] = True
            rec["manualEntry"] = True
            stock_closed.append(rec)

    for lst in (csp_closed, leap_closed, spread_closed, covered_closed, stock_closed):
        lst.sort(key=lambda r: r["closedAt"], reverse=True)
    return {"csp": csp_closed, "leap": leap_closed, "spread": spread_closed, "covered": covered_closed,
            "stock": stock_closed, "_unresolved": unresolved}


def _load_txns(data_dir: str) -> dict[str, list[dict[str, Any]]]:
    """Load the transactions feed (for stock reconstruction). Empty if absent."""
    try:
        with open(os.path.join(data_dir, TXNS_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_manual_basis(data_dir: str) -> dict[str, dict[str, Any]]:
    """User-entered basis for stock sales whose purchase predates the feed, keyed by
    orphan id. Written by the app to manual_cost_basis.json. Accepts a bare number
    ({id: costPerShare}) or an object ({id: {"costPerShare": x, "acquiredDate": iso}}).
    acquiredDate (when present) sets the real holding period so the sale can be
    classified short- vs long-term. Returns {id: {"costPerShare": float,
    "acquiredDate": str|None}}."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with open(os.path.join(data_dir, MANUAL_BASIS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if isinstance(v, dict):
            cps = v.get("costPerShare")
            acq = v.get("acquiredDate")
        else:
            cps, acq = v, None
        if isinstance(cps, (int, float)):
            out[k] = {"costPerShare": float(cps), "acquiredDate": acq if isinstance(acq, str) and acq else None}
    return out


def _load_manual_sales(data_dir: str) -> list[dict[str, Any]]:
    """Fully user-added closed stock sales (predate the feed), written by the app to
    manual_stock_sales.json as a list of
    {symbol, shares, proceedsPerShare, costPerShare, acquiredDate, soldDate}."""
    try:
        with open(os.path.join(data_dir, MANUAL_SALES_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def write_closed(data_dir: str, store: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    txns_store = _load_txns(data_dir)
    manual_basis = _load_manual_basis(data_dir)
    manual_sales = _load_manual_sales(data_dir)
    closed = build_from_history(store, txns_store, manual_basis, manual_sales)
    now = datetime.now(timezone.utc).isoformat()
    note = "Options reconstructed from order history (FIFO); stocks from the transactions feed incl. assignment cost basis. Spreads are same-underlying/type/expiration verticals."

    def dump(filename: str, key: str, wrapper: str = "closed") -> None:
        with open(os.path.join(data_dir, filename), "w", encoding="utf-8") as f:
            json.dump({"meta": {"generatedAt": now, "source": SOURCE_LABEL, "note": note}, wrapper: closed[key]}, f, indent=2)

    dump(CSP_FILE, "csp")
    dump(LEAPS_FILE, "leap")
    dump(SPREADS_FILE, "spread")
    dump(COVERED_FILE, "covered")
    dump(STOCKS_FILE, "stock")
    dump(UNRESOLVED_FILE, "_unresolved", wrapper="unresolved")
    return {k: len(v) for k, v in closed.items() if not k.startswith("_")}


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    data_dir = _data_dir()
    path = os.path.join(data_dir, HISTORY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, json.JSONDecodeError):
        raise SystemExit(f"No readable {HISTORY_FILE} in {data_dir}. Run sync_trade_history.py first.")
    counts = write_closed(data_dir, store)
    print(
        f"Wrote closed files — CSPs: {counts['csp']}, LEAPs: {counts['leap']}, "
        f"spreads: {counts['spread']}, covered calls: {counts['covered']}, stocks: {counts['stock']}."
    )


if __name__ == "__main__":
    main()
