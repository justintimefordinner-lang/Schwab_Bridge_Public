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
SOURCE_LABEL = "schwab-bridge"

OPEN_INSTRUCTIONS = {"SELL_TO_OPEN", "BUY_TO_OPEN"}
CLOSE_INSTRUCTIONS = {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}


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
                "time": o.get("enteredTime", "") or "",
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


def _build_csp(t: dict[str, Any], fpc: Callable[[Any], float] = _no_fee) -> dict[str, Any] | None:
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
        "returnOnCollateral": _round(roc, 4), "annualized": _round(roc * 365 / days, 4),
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


def _build_covered_call(t: dict[str, Any], fpc: Callable[[Any], float] = _no_fee) -> dict[str, Any] | None:
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


def _combine_spreads(s_trips: list[dict[str, Any]], l_trips: list[dict[str, Any]], fpc: Callable[[Any], float] = _no_fee) -> list[dict[str, Any]]:
    """Pair a spread's short-leg and long-leg round-trips (they open/close
    together) by chronological order, and value each as one spread."""
    out = []
    for s, l in zip(sorted(s_trips, key=lambda t: t["open_time"]),
                    sorted(l_trips, key=lambda t: t["open_time"])):
        rec = _build_spread(s, l, fpc)
        if rec:
            out.append(rec)
    return out


_EQUITY_ASSET_TYPES = {"EQUITY", "COLLECTIVE_INVESTMENT"}


def _equity_events_from_txns(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per-symbol chronological equity events from the TRANSACTIONS feed.

    Two sources, so assignment cost basis is captured (orders miss it):
      • TRADE with an EQUITY/ETF item → a real buy/sell. positionEffect + the
        sign of `amount` give open/close and long/short.
      • RECEIVE_AND_DELIVER with an OPTION item → an assignment. A PUT assignment
        means shares were put to you (BUY at strike); a CALL assignment means
        shares were called away (SELL at strike). Strike = cost/sale basis.
    """
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for t in records:
        ttype = (t.get("type") or "").upper()
        when = t.get("tradeDate") or t.get("time") or ""
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
                by_sym.setdefault(sym, []).append(
                    {"time": when, "instruction": instruction, "qty": abs(amount), "price": price}
                )

            elif ttype == "RECEIVE_AND_DELIVER" and atype == "OPTION":
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
                # PUT assigned → buy shares at strike; CALL assigned → sell at strike.
                instruction = "BUY" if pc == "PUT" else "SELL"
                by_sym.setdefault(underlying, []).append(
                    {"time": when, "instruction": instruction, "qty": shares, "price": strike}
                )
    return by_sym


def _equity_round_trips(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO-match equity opens to closes. Longs: BUY opens, SELL closes. Shorts:
    SELL_SHORT opens, BUY_TO_COVER closes. Long and short lots queue separately."""
    evs = sorted(evs, key=lambda e: e["time"])
    long_lots: deque[dict[str, Any]] = deque()
    short_lots: deque[dict[str, Any]] = deque()
    trips: list[dict[str, Any]] = []

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
    return trips


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
        "daysHeld": days, "returnPct": _round(ret, 4), "annualized": _round(ret * 365 / days, 4),
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


def build_from_history(
    store: dict[str, list[dict[str, Any]]],
    txns_store: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list]:
    today = datetime.now(timezone.utc).date()
    csp_closed: list[dict[str, Any]] = []
    leap_closed: list[dict[str, Any]] = []
    spread_closed: list[dict[str, Any]] = []
    covered_closed: list[dict[str, Any]] = []
    stock_closed: list[dict[str, Any]] = []

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

    # Options come from the ORDERS feed.
    for records in store.values():
        spread_occs, pairs = _spread_partners(records)
        trips_by_occ = {occ: _fifo_round_trips(evs, today) for occ, evs in _events_for_contract(records).items()}

        # Spreads: combine each detected short/long pair into one round-trip.
        for short_occ, long_occ in pairs:
            spread_closed.extend(_combine_spreads(trips_by_occ.get(short_occ, []), trips_by_occ.get(long_occ, []), fpc))

        # Everything that ISN'T a spread leg classifies on its own.
        for occ, trips in trips_by_occ.items():
            if occ in spread_occs:
                continue
            for t in trips:
                is_put = t["putCall"] in ("PUT", "P")
                if t["short"] and is_put:
                    rec = _build_csp(t, fpc)             # naked short put = CSP
                    if rec:
                        csp_closed.append(rec)
                elif t["short"] and not is_put:
                    rec = _build_covered_call(t, fpc)    # short call = covered call
                    if rec:
                        covered_closed.append(rec)
                elif not t["short"]:
                    rec = _build_leap(t, fpc)            # long call/put = LEAP
                    if rec:
                        leap_closed.append(rec)

    # Stocks come from the TRANSACTIONS feed (captures assignment cost basis).
    for records in (txns_store or {}).values():
        for sym, evs in _equity_events_from_txns(records).items():
            for i, t in enumerate(_equity_round_trips(evs)):
                rec = _build_stock(t, sym, i)
                if rec:
                    stock_closed.append(rec)

    for lst in (csp_closed, leap_closed, spread_closed, covered_closed, stock_closed):
        lst.sort(key=lambda r: r["closedAt"], reverse=True)
    return {"csp": csp_closed, "leap": leap_closed, "spread": spread_closed, "covered": covered_closed, "stock": stock_closed}


def _load_txns(data_dir: str) -> dict[str, list[dict[str, Any]]]:
    """Load the transactions feed (for stock reconstruction). Empty if absent."""
    try:
        with open(os.path.join(data_dir, TXNS_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_closed(data_dir: str, store: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    txns_store = _load_txns(data_dir)
    closed = build_from_history(store, txns_store)
    now = datetime.now(timezone.utc).isoformat()
    note = "Options reconstructed from order history (FIFO); stocks from the transactions feed incl. assignment cost basis. Spreads are same-underlying/type/expiration verticals."

    def dump(filename: str, items: list) -> None:
        with open(os.path.join(data_dir, filename), "w", encoding="utf-8") as f:
            json.dump({"meta": {"generatedAt": now, "source": SOURCE_LABEL, "note": note}, "closed": items}, f, indent=2)

    dump(CSP_FILE, closed["csp"])
    dump(LEAPS_FILE, closed["leap"])
    dump(SPREADS_FILE, closed["spread"])
    dump(COVERED_FILE, closed["covered"])
    dump(STOCKS_FILE, closed["stock"])
    return {k: len(v) for k, v in closed.items()}


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
