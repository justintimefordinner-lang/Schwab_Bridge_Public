"""
manual_positions.py — price hand-entered positions with Schwab market data.

Positions held somewhere Schwab can't see (another broker, a 401k window, a
paper account) are entered on the dashboard's Settings page, which writes
them to APP_DATA_DIR/manual_positions.json with only the facts Schwab can't
know: symbol, quantity, strike, expiration, what you paid or collected.

Each cycle this module reads that file, asks Schwab for the underlying quote
and the option contract's own quote (mark + delta/gamma/theta/vega/IV — the
same call export_to_app makes for held contracts), and writes
APP_DATA_DIR/manual/snapshot.json in the exact shape the Schwab export
writes. The dashboard merges every subfolder's snapshot, so the manual
accounts appear in the account switcher, the Combined view, Options, P&L
and the Positions table with no reader changes.

Input (written by the app, never edited here):

    {"version": 1, "accounts": [{
        "id": "manual-etrade", "label": "E*TRADE", "cash": 12000,
        "positions": [
          {"id": "…", "type": "stock",  "symbol": "AAPL", "qty": 100, "avgCost": 240, "openedAt": "2026-08-01"},
          {"id": "…", "type": "option", "symbol": "SOFI", "optionType": "put", "side": "short",
           "qty": 1, "strike": 16, "expiration": "2026-10-09", "premium": 0.69, "openedAt": "2026-09-18"}
        ]}]}

Strategy labels follow classify_positions' rules: a short put is a CSP, a
long call a LEAP, a short call covered by 100 shares/contract of the same
name in the same manual account is a covered call, a short+long pair at one
expiration is a vertical spread, a long-dated long put a hedge.

Runs as the "manual" target in auto_push (MANUAL_PUSH_INTERVAL, default =
APP_PUSH_INTERVAL). With no manual file, or no accounts in it, the output
snapshot is removed so a deleted account disappears from the dashboard.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any

MANUAL_FILE = "manual_positions.json"
OUT_DIR = "manual"
OUT_FILE = "snapshot.json"
EARNINGS_FILE = "earnings.json"


# --- OCC symbols -------------------------------------------------------------
def occ_symbol(symbol: str, expiration: str, option_type: str, strike: float) -> str:
    """Schwab's OCC form: root padded to 6, YYMMDD, P/C, strike×1000 in 8 digits.
    'SOFI', '2026-10-09', 'put', 16 -> 'SOFI  261009P00016000'."""
    root = symbol.strip().upper().ljust(6)
    yymmdd = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    pc = "P" if option_type.lower().startswith("p") else "C"
    return f"{root}{yymmdd}{pc}{int(round(float(strike) * 1000)):08d}"


def _dte(expiration: str) -> int | None:
    try:
        return (date.fromisoformat(expiration) - date.today()).days
    except ValueError:
        return None


# --- input -------------------------------------------------------------------
def load_manual(data_dir: str) -> list[dict[str, Any]]:
    """Manual accounts from the app's file; [] when absent or malformed."""
    path = os.path.join(data_dir, MANUAL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    accounts = doc.get("accounts") if isinstance(doc, dict) else None
    return [a for a in (accounts or []) if isinstance(a, dict) and a.get("id")]


def _clean_positions(acct: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Split one account's rows into (stocks, options), dropping anything
    unusable rather than failing the whole account on one bad row."""
    stocks: list[dict] = []
    options: list[dict] = []
    for p in acct.get("positions") or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        try:
            qty = float(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0:
            continue
        if p.get("type") == "stock":
            try:
                avg = float(p.get("avgCost") or 0)
            except (TypeError, ValueError):
                avg = 0.0
            stocks.append({"id": p.get("id"), "symbol": sym, "qty": qty, "avgCost": avg, "openedAt": p.get("openedAt")})
        elif p.get("type") == "option":
            try:
                strike = float(p.get("strike"))
                premium = abs(float(p.get("premium") or 0))
                exp = str(p.get("expiration"))
                datetime.strptime(exp, "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            ot = "put" if str(p.get("optionType", "")).lower().startswith("p") else "call"
            side = "short" if str(p.get("side", "")).lower().startswith("s") else "long"
            options.append({
                "id": p.get("id"), "symbol": sym, "optionType": ot, "side": side, "qty": int(qty),
                "strike": strike, "expiration": exp, "premium": premium, "openedAt": p.get("openedAt"),
            })
    return stocks, options


# --- classification ------------------------------------------------------------
def categorize(stocks: list[dict], options: list[dict]) -> dict[str, str]:
    """{option id: category} using the same vocabulary export_to_app._kind_for
    reads: CSPs / LEAPS / Covered calls / Put spreads / Call spreads / Other."""
    shares: dict[str, float] = {}
    for s in stocks:
        shares[s["symbol"]] = shares.get(s["symbol"], 0.0) + s["qty"]

    # Verticals: a short and a long of the same type at one expiration.
    by_key: dict[tuple, list[dict]] = {}
    for o in options:
        by_key.setdefault((o["symbol"], o["optionType"], o["expiration"]), []).append(o)

    cats: dict[str, str] = {}
    for (sym, ot, _exp), legs in by_key.items():
        sides = {leg["side"] for leg in legs}
        if "short" in sides and "long" in sides:
            for leg in legs:
                cats[leg["id"]] = "Put spreads" if ot == "put" else "Call spreads"
            continue
        for leg in legs:
            if ot == "put" and leg["side"] == "short":
                cats[leg["id"]] = "CSPs"
            elif ot == "call" and leg["side"] == "long":
                cats[leg["id"]] = "LEAPS"
            elif ot == "call" and leg["side"] == "short":
                need = 100 * leg["qty"]
                if shares.get(sym, 0.0) >= need:
                    shares[sym] -= need
                    cats[leg["id"]] = "Covered calls"
                else:
                    cats[leg["id"]] = "Other"
            else:
                cats[leg["id"]] = "Other"  # a long put; _kind_for makes a hedge of a long-dated one
    return cats


# --- earnings dates (only for tickers the roster feed hasn't covered) ---------
def _fill_missing_earnings(data_dir: str, tickers: list[str]) -> None:
    path = os.path.join(data_dir, EARNINGS_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            have = json.load(f)
    except (OSError, json.JSONDecodeError):
        have = {}
    missing = sorted(t for t in tickers if t not in have)
    if not missing:
        return
    try:
        import fetch_earnings
        fetch_earnings.main(["fetch_earnings.py", *missing])
    except Exception as exc:  # yfinance absent, network, etc. — the flag just stays off
        print(f"  note: earnings lookup skipped ({exc}).")


# --- main --------------------------------------------------------------------
def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    import export_to_app as ex
    import schwab_client as sc

    data_dir = ex._app_data_dir()
    out_dir = os.path.join(data_dir, OUT_DIR)
    out_path = os.path.join(out_dir, OUT_FILE)

    accounts = load_manual(data_dir)
    if not accounts:
        # Nothing to price. Drop a stale output so a removed account goes away.
        if os.path.exists(out_path):
            os.remove(out_path)
            print("manual: no accounts — removed stale snapshot")
        return

    # One pass over everything first, so a single quotes call covers every account.
    cleaned = {a["id"]: _clean_positions(a) for a in accounts}
    occs: dict[str, str] = {}
    tickers: set[str] = set()
    for stocks, options in cleaned.values():
        for s in stocks:
            tickers.add(s["symbol"])
        for o in options:
            tickers.add(o["symbol"])
            o["occ"] = occ_symbol(o["symbol"], o["expiration"], o["optionType"], o["strike"])
            occs[o["occ"]] = o["symbol"]

    c = sc.get_client()
    try:
        greeks = ex.get_option_greeks(c, sorted(occs)) if occs else {}
    except Exception as exc:
        print(f"  note: option quotes unavailable ({exc}); marks fall back to entry.")
        greeks = {}
    stock_day = ex.fetch_stock_day(c, sorted(tickers)) if tickers else {}

    history = ex.load_history(data_dir)
    today = date.today().isoformat()
    prices_as_of = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    app_accounts: list[dict[str, Any]] = []
    data_by_account: dict[str, dict[str, Any]] = {}
    for acct in accounts:
        acct_id = str(acct["id"])
        stocks, options = cleaned[acct_id]
        cats = categorize(stocks, options)
        try:
            cash = float(acct.get("cash") or 0)
        except (TypeError, ValueError):
            cash = 0.0

        equities = []
        for s in stocks:
            sd = stock_day.get(s["symbol"], {})
            price = sd.get("price")
            equities.append(ex.map_equity({
                "ticker": s["symbol"], "quantity": s["qty"], "avg_price": s["avgCost"],
                "underlying_price": price if price is not None else s["avgCost"],
                "category": "Stock",
            }, stock_day))

        opts = []
        open_dates: dict[str, str] = {}
        for o in options:
            g = greeks.get(o["occ"], {})
            sd = stock_day.get(o["symbol"], {})
            signed_qty = -o["qty"] if o["side"] == "short" else o["qty"]
            p = {
                "symbol": o["occ"], "ticker": o["symbol"],
                "put_call": "PUT" if o["optionType"] == "put" else "CALL",
                "quantity": signed_qty, "avg_price": o["premium"], "strike": o["strike"],
                "expiration": o["expiration"], "dte": _dte(o["expiration"]),
                "underlying_price": sd.get("price") if sd.get("price") is not None else g.get("underClose"),
                "category": cats.get(o["id"], "Other"),
                "day_pl": None, "theta": None,
                # No quote for this contract (typo'd strike, expired, market-data
                # product missing): value it at what was paid so P&L reads flat, not $0.
                "market_value": (o["premium"] * 100 * o["qty"]) if g.get("mark") is None else None,
            }
            if o.get("openedAt"):
                open_dates[o["occ"]] = str(o["openedAt"])
            row = ex.map_option(p, greeks, open_dates, stock_day)
            row["id"] = f"{acct_id}:{o['id'] or o['occ']}"
            opts.append(row)

        equity_value = sum((e["qty"] or 0) * (e["price"] or 0) for e in equities)
        options_net = sum(
            (r["mark"] or 0) * 100 * r["qty"] * (1 if r["side"] == "long" else -1) for r in opts
        )
        total = cash + equity_value + options_net
        points = ex.update_history(history, acct_id, total, today)

        app_accounts.append({
            "id": acct_id,
            "mask": "manual",
            "type": "manual",
            "brokerageType": "manual",
            "nickname": str(acct.get("label") or "Manual"),
            "isDefault": False,
        })
        data_by_account[acct_id] = {
            "summary": {
                "totalValue": ex._round(total),
                "equityValue": ex._round(equity_value),
                "optionsValue": ex._round(options_net),
                "cryptoValue": 0.0,
                "cash": ex._round(cash),
                "buyingPower": ex._round(cash),
                "optionsBuyingPower": ex._round(cash),
            },
            "equities": equities,
            "options": opts,
            "valueHistory": points,
        }
        print(f"manual: {acct.get('label')} — {len(equities)} stocks, {len(opts)} options, "
              f"{sum(1 for o in options if greeks.get(o['occ'], {}).get('mark') is not None)} contracts quoted")

    snapshot = ex.build_snapshot(app_accounts, data_by_account, prices_as_of)
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    os.replace(tmp, out_path)
    ex.save_history(data_dir, history)

    # Decorations the roster-driven feeds would otherwise miss for these names.
    try:
        import sectors as _sectors
        _sectors.refresh_sectors(data_dir, sorted(tickers))
    except Exception as exc:
        print(f"  note: sector lookup skipped ({exc}).")
    _fill_missing_earnings(data_dir, sorted({o["symbol"] for _s, os_ in cleaned.values() for o in os_}))


if __name__ == "__main__":
    main()
