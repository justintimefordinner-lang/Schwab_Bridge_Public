"""
schwab_client.py
================

Read-only data layer. Everything the dashboard needs to display lives
here, normalized into plain dicts and lists so the UI never has to know
about Schwab's raw JSON shape.

This module deliberately contains NO trade-placing or order-cancelling
calls. Trade execution, if added later, belongs in a separate module so
the read-only surface stays small and auditable.

NOTE ON METHOD NAMES: schwab-py occasionally adjusts method signatures
between versions. The three client methods used below are:
    client.get_account_numbers()
    client.get_account(account_hash, fields=...)
    client.get_orders_for_account(account_hash, from_entered_datetime=...)
If you hit an AttributeError, run `pip show schwab-py` and check the
matching docs at https://schwab-py.readthedocs.io/en/latest/client.html
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from schwab import auth, client

load_dotenv()

_API_KEY = os.environ.get("SCHWAB_API_KEY")
_APP_SECRET = os.environ.get("SCHWAB_APP_SECRET")
_TOKEN_PATH = os.environ.get("SCHWAB_TOKEN_PATH", "token.json")


class AuthError(RuntimeError):
    """Raised when the token is missing or can no longer be refreshed."""


def get_client() -> client.Client:
    """Build a client from the cached token file.

    Does NOT trigger an interactive login. If the token is missing or
    expired, raises AuthError.

    Credentials are re-read on every call (base config from .env, App
    Key/Secret from credentials.env if present) so a long-running bridge
    picks up first-run credentials saved from the dashboard without a
    restart.
    """
    load_dotenv(".env")
    load_dotenv(os.environ.get("SCHWAB_CREDENTIALS_PATH", "credentials.env"), override=True)
    api_key = os.environ.get("SCHWAB_API_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "token.json")

    if not api_key or not app_secret:
        raise AuthError(
            "Missing credentials. Add your Schwab App Key and Secret from the "
            "dashboard Settings page (or fill in credentials.env)."
        )
    if not os.path.exists(token_path):
        raise AuthError(
            f"No token at '{token_path}'. Reconnect Schwab from the dashboard "
            "Settings page."
        )
    try:
        return auth.client_from_token_file(token_path, api_key, app_secret)
    except Exception as exc:  # schwab-py raises on a dead refresh token
        raise AuthError(
            "Could not load or refresh the token (it may have expired). "
            "Reconnect Schwab from the dashboard Settings page."
        ) from exc


def list_accounts(c: client.Client) -> list[dict[str, str]]:
    """Return [{'number': '...masked', 'hash': '...'}] for linked accounts.

    The hash is what every other API call uses; the plain number is only
    for display. We mask all but the last 4 digits.
    """
    resp = c.get_account_numbers()
    resp.raise_for_status()
    out = []
    for item in resp.json():
        number = str(item.get("accountNumber", ""))
        out.append(
            {
                "number": f"****{number[-4:]}" if len(number) >= 4 else number,
                "hash": item.get("hashValue", ""),
            }
        )
    return out


def get_quotes(c: client.Client, symbols: list[str]) -> dict[str, float]:
    """Return {symbol: current price} for the given underlying symbols.

    Uses Schwab's market data quotes endpoint. Price is the last trade,
    falling back to mark then prior close. Requires the "Market Data
    Production" product on your Schwab app; without it the call fails and
    the caller surfaces that quotes are unavailable.
    """
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return {}
    resp = c.get_quotes(wanted)
    resp.raise_for_status()
    data = resp.json() or {}
    prices: dict[str, float] = {}
    for sym, payload in data.items():
        quote = (payload or {}).get("quote", {}) or {}
        price = quote.get("lastPrice")
        if price is None:
            price = quote.get("mark")
        if price is None:
            price = quote.get("closePrice")
        if price is not None:
            prices[sym] = price
    return prices


def get_price_history(c: client.Client, symbol: str, days: int = 400) -> list[dict[str, Any]]:
    """Daily OHLCV candles for the last ~`days` calendar days (≈1y of trading
    days at the default). Returns a list of {open,high,low,close,volume,datetime}
    dicts, oldest first; empty on any error so one bad symbol can't break a run.

    Schwab serves no technical indicators — these candles are the raw material
    the indicators module turns into Bollinger/RSI/MACD.
    """
    start = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        resp = c.get_price_history_every_day(symbol, start_datetime=start, need_extended_hours_data=False)
        resp.raise_for_status()
    except Exception:
        return []
    candles = (resp.json() or {}).get("candles", []) or []
    return candles


# VIX Cash Allocation framework (Options Trading University / Ryan Hildreth).
# Each band is [low, high) on the VIX with target cash and invested ranges.
# The 25-30 gap in the source is folded into the Fear band so no VIX is left
# unclassified.
VIX_GUIDE = [
    {"low": 0.0, "high": 12.0, "regime": "Extreme Greed",
     "cash_low": 40, "cash_high": 50, "cash": "40-50%", "invested": "50-60%"},
    {"low": 12.0, "high": 15.0, "regime": "Greed",
     "cash_low": 30, "cash_high": 40, "cash": "30-40%", "invested": "60-70%"},
    {"low": 15.0, "high": 20.0, "regime": "Slight Fear",
     "cash_low": 20, "cash_high": 25, "cash": "20-25%", "invested": "75-80%"},
    {"low": 20.0, "high": 30.0, "regime": "Fear",
     "cash_low": 10, "cash_high": 15, "cash": "10-15%", "invested": "90-95%"},
    {"low": 30.0, "high": 1000.0, "regime": "Extreme Fear",
     "cash_low": 0, "cash_high": 5, "cash": "0-5%", "invested": "95-100%",
     "note": "Find $$$!"},
]


def vix_band(vix: float | None) -> dict | None:
    """Return the full guide band a VIX value falls in, or None."""
    if vix is None:
        return None
    for band in VIX_GUIDE:
        if band["low"] <= vix < band["high"]:
            return band
    return None


def vix_regime(vix: float | None) -> str | None:
    """Return the regime label for a VIX value, or None if unavailable."""
    band = vix_band(vix)
    return band["regime"] if band else None


def vix_recommendation(
    cash: float | None, liquidation_value: float | None, vix: float | None
) -> dict | None:
    """Compare current cash to the framework's target for the VIX regime.

    Returns the current cash percent, the target range, and an action
    sentence (deploy, raise cash, or hold). None if VIX or account value
    is unavailable.
    """
    band = vix_band(vix)
    if not band or not liquidation_value:
        return None
    cash_pct = (cash or 0) / liquidation_value * 100
    lo, hi, target, regime = band["cash_low"], band["cash_high"], band["cash"], band["regime"]
    tol = 2.0  # within 2 percentage points of the band counts as on target
    if cash_pct > hi + tol:
        amount = (cash_pct - hi) / 100 * liquidation_value
        action = f"Available: {cash_pct:.1f}%, Deploy ${amount:,.0f}"
        if band.get("note"):
            action += f", {band['note']}"
        stance = "deploy"
    elif cash_pct < lo - tol:
        amount = (lo - cash_pct) / 100 * liquidation_value
        action = f"Available: {cash_pct:.1f}%, Raise ${amount:,.0f}"
        stance = "raise"
    else:
        action = f"Available: {cash_pct:.1f}%, On Target"
        stance = "hold"
    return {"cash_pct": cash_pct, "target": target, "regime": regime,
            "action": action, "stance": stance}


# Candidate Schwab balance fields for "options buying power", in priority
# order. The first present, non-null value wins. If your account exposes a
# different field for options buying power, reorder this list.
OPTIONS_BP_FIELDS = [
    "optionBuyingPower",
    "buyingPowerNonMarginableTrade",
    "availableFundsNonMarginableTrade",
    "cashAvailableForTrading",
    "availableFunds",
    "buyingPower",
    "cashBalance",
]


def options_buying_power(balances: dict[str, Any]) -> tuple[float | None, str | None]:
    """Resolve options buying power from account balances.

    Returns (value, field_used), trying OPTIONS_BP_FIELDS in order so the
    figure reflects deployable buying power rather than cash tied up as
    collateral.
    """
    for field in OPTIONS_BP_FIELDS:
        value = balances.get(field)
        if value is not None:
            return value, field
    return None, None


def get_vix(c: client.Client) -> float | None:
    """Current CBOE Volatility Index level via Schwab market data.

    Returns None if the quote can't be retrieved (e.g. the Market Data
    product isn't enabled). Tries the common index price fields.
    """
    resp = c.get_quotes(["$VIX"])
    resp.raise_for_status()
    data = resp.json() or {}
    payload = data.get("$VIX") or data.get("VIX") or {}
    quote = payload.get("quote", {}) or {}
    for key in ("lastPrice", "mark", "closePrice", "lastPriceInDouble", "last"):
        value = quote.get(key)
        if value is not None:
            return value
    return None


def get_option_thetas(c: client.Client, option_symbols: list[str]) -> dict[str, float]:
    """Return {option_symbol: theta per share} from Schwab market data.

    Theta is the option's daily time decay. Schwab reports it negative (an
    option loses that much value per day). Requires the Market Data product.
    """
    wanted = sorted({s for s in option_symbols if s})
    if not wanted:
        return {}
    resp = c.get_quotes(wanted)
    resp.raise_for_status()
    data = resp.json() or {}
    thetas: dict[str, float] = {}
    for sym, payload in data.items():
        quote = (payload or {}).get("quote", {}) or {}
        theta = quote.get("theta")
        if theta is not None:
            thetas[sym] = theta
    return thetas


def _days_to_expiration(expiration_iso: str | None) -> int | None:
    """Whole days from today until an option's expiration (ISO date).

    None for stocks or any leg whose expiration could not be parsed.
    """
    if not expiration_iso:
        return None
    try:
        exp = date.fromisoformat(expiration_iso)
    except (ValueError, TypeError):
        return None
    return (exp - date.today()).days


def _capital_efficiency(position: dict[str, Any]) -> float | None:
    """CSP-only annualized return on collateral, as a percentage.

    (360 / DTE) x current option value / collateral x 100, where the
    current option value is the put's current price x contracts x 100,
    i.e. the magnitude of the position's market value. A 30-day put worth
    about 3.5% of its collateral scores about 42%. Returns None outside of
    CSPs or when DTE/collateral are missing or non-positive.
    """
    if position.get("category") != "CSPs":
        return None
    dte = position.get("dte")
    collateral = position.get("alloc_value") or 0
    option_value = abs(position.get("market_value") or 0)
    if not dte or dte <= 0 or collateral <= 0:
        return None
    return (360.0 / dte) * option_value / collateral * 100.0


def get_account_snapshot(c: client.Client, account_hash: str) -> dict[str, Any]:
    """Return balances + parsed positions for one account."""
    fields = client.Client.Account.Fields.POSITIONS
    resp = c.get_account(account_hash, fields=fields)
    resp.raise_for_status()
    data = resp.json().get("securitiesAccount", {})

    balances = data.get("currentBalances", {})
    raw_positions = data.get("positions", [])
    positions = classify_positions(raw_positions)
    day_pl = sum(p["day_pl"] for p in positions)
    open_pl = sum(p["open_pl"] for p in positions)

    # Allocation by capital committed, built from the classified positions
    # (spread-aware). Long spread legs carry 0, so each spread's collateral
    # is counted once, on its short leg.
    allocation: dict[str, float] = {}
    by_ticker: dict[str, float] = {}
    for p in positions:
        value = p["alloc_value"] or 0
        if value:
            allocation[p["category"]] = allocation.get(p["category"], 0.0) + value
            by_ticker[p["ticker"]] = by_ticker.get(p["ticker"], 0.0) + value

    # Live underlying price for every ticker held (so option-only names
    # show a current price too, not just tickers where we hold shares).
    underlying_prices: dict[str, float] = {}
    quotes_error: str | None = None
    tickers = sorted({p["ticker"] for p in positions if p.get("ticker")})
    if tickers:
        try:
            underlying_prices = get_quotes(c, tickers)
        except Exception:  # most often: Market Data product not enabled
            quotes_error = (
                "Live quotes unavailable. Add the 'Market Data Production' "
                "product to your app at developer.schwab.com, then re-run "
                "auth_setup.py."
            )
    # Option thetas (daily time decay) for every option position. Same market
    # data product as quotes.
    option_symbols = sorted(
        {p["symbol"] for p in positions
         if (p.get("asset_type") or "").upper() == "OPTION"}
    )
    thetas: dict[str, float] = {}
    if option_symbols:
        try:
            thetas = get_option_thetas(c, option_symbols)
        except Exception:
            quotes_error = quotes_error or (
                "Live quotes unavailable. Add the 'Market Data Production' "
                "product to your app at developer.schwab.com, then re-run "
                "auth_setup.py."
            )

    for p in positions:
        p["underlying_price"] = underlying_prices.get(p["ticker"])
        p["dte"] = _days_to_expiration(p.get("expiration"))
        p["capital_efficiency"] = _capital_efficiency(p)
        theta_ps = thetas.get(p["symbol"])
        p["theta"] = theta_ps  # per share, as reported
        if theta_ps is not None and (p.get("asset_type") or "").upper() == "OPTION":
            # Daily $ decay to the holder: positive for shorts (captured),
            # negative for longs (paid). Schwab reports theta negative.
            p["theta_dollars"] = theta_ps * (p["quantity"] or 0) * 100
        else:
            p["theta_dollars"] = None

    # Theta roll-ups.
    theta_total = sum(p["theta_dollars"] or 0 for p in positions)
    theta_by_ticker: dict[str, float] = {}
    for p in positions:
        if p["theta_dollars"]:
            theta_by_ticker[p["ticker"]] = (
                theta_by_ticker.get(p["ticker"], 0.0) + p["theta_dollars"]
            )
    liq_value = balances.get("liquidationValue") or 0
    theta_annual_pct = (theta_total * 365 / liq_value * 100) if liq_value else None

    # Current VIX level (same market data product as quotes).
    try:
        vix = get_vix(c)
    except Exception:
        vix = None

    # Options buying power drives the VIX allocation, not raw cash (which can
    # be tied up as collateral).
    options_bp, options_bp_field = options_buying_power(balances)

    return {
        "account_type": data.get("type", ""),
        "liquidation_value": balances.get("liquidationValue"),
        "cash": balances.get("cashBalance"),
        # Buying power field differs between margin and cash accounts.
        "buying_power": balances.get("buyingPower")
        or balances.get("cashAvailableForTrading"),
        "options_bp": options_bp,
        "options_bp_field": options_bp_field,
        "day_pl": day_pl,
        "open_pl": open_pl,
        "theta_total": theta_total,
        "theta_by_ticker": theta_by_ticker,
        "theta_annual_pct": theta_annual_pct,
        "vix": vix,
        "vix_regime": vix_regime(vix),
        "vix_reco": vix_recommendation(
            options_bp, balances.get("liquidationValue"), vix
        ),
        "positions": positions,
        "allocation": allocation,
        "by_ticker": by_ticker,
        "underlying_prices": underlying_prices,
        "quotes_error": quotes_error,
    }


def _classify_single_leg(leg: dict[str, Any]) -> tuple[str, float]:
    """Category + allocation value for a standalone option leg (no
    offsetting leg of the same type on the same underlying)."""
    pc = (leg["put_call"] or "").upper()
    is_put = pc in ("PUT", "P")
    is_call = pc in ("CALL", "C")
    short_qty = leg["_short"] or 0
    long_qty = leg["_long"] or 0
    mv = leg["market_value"] or 0
    strike = leg["strike"]
    if is_put and short_qty > 0:
        if strike is not None:
            return "CSPs", strike * 100 * short_qty
        return "CSPs", abs(mv)
    if is_call and long_qty > 0:
        return "LEAPS", mv
    return "Other", abs(mv)


def classify_positions(raw_positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse every position and assign a category + allocation value,
    detecting spreads across the whole account rather than leg by leg.

    Spread detection is vertical-only: on a given underlying and option
    type, a short and a long sharing an expiration form a vertical spread.
    Legs at different expirations are NOT paired (no calendar/diagonal), so
    a stand-alone long call stays a long call and a short call is judged on
    its own.

    Covered calls: a leftover short call is classified as a covered call
    when enough long shares of the underlying are held to cover it (100
    shares per contract); its capital sits in the stock, so the call itself
    carries 0 allocation. An uncovered short call falls to Other.

    Spread valuation (per vertical, using the legs' opening premiums):
      - Credit spread (collected more than paid): defined risk =
        (strike width - net credit) x 100 x contracts, on the short leg.
      - Debit spread (paid more): net debit paid x 100 x contracts, on the
        long leg.
    Credit vs debit uses premium magnitudes, independent of Schwab's sign
    convention. Naked short puts stay CSPs; lone long calls stay LEAPS; lone
    long puts fall to Other. The non-carrying leg holds 0 so each spread
    counts once.

    NOTE: assumes premiums come from averagePrice per share, and a single
    vertical per underlying/type/expiration.
    """
    legs: list[dict[str, Any]] = []
    for p in raw_positions:
        instrument = p.get("instrument", {})
        symbol = instrument.get("symbol", "")
        long_qty = p.get("longQuantity", 0) or 0
        short_qty = p.get("shortQuantity", 0) or 0
        avg_price = p.get("averagePrice")
        market_value = p.get("marketValue")
        asset = (instrument.get("assetType") or "").upper()
        mult = 100 if asset == "OPTION" else 1

        # Overall (unrealized) P&L since opening. Prefer Schwab's own fields;
        # otherwise derive it from current value vs cost basis, by direction.
        if ("longOpenProfitLoss" in p) or ("shortOpenProfitLoss" in p):
            open_pl = (p.get("longOpenProfitLoss") or 0) + (p.get("shortOpenProfitLoss") or 0)
        else:
            mv = market_value or 0
            avg = avg_price or 0
            if long_qty:
                open_pl = mv - avg * long_qty * mult          # long: value minus cost
            elif short_qty:
                open_pl = avg * short_qty * mult - abs(mv)     # short: proceeds minus value
            else:
                open_pl = 0.0
        basis = (avg_price or 0) * (long_qty or short_qty) * mult
        open_pl_pct = (open_pl / basis * 100) if basis else 0.0

        # Opening premium as a cash flow: positive received (short), negative
        # paid (long). None for non-options (stock has no premium).
        signed_qty = long_qty - short_qty
        premium = (-signed_qty * (avg_price or 0) * mult) if asset == "OPTION" else None

        legs.append(
            {
                "symbol": symbol,
                "ticker": _underlying_symbol(instrument),
                "asset_type": instrument.get("assetType", ""),
                "put_call": instrument.get("putCall", ""),
                "quantity": signed_qty,
                "avg_price": avg_price,
                "premium": premium,
                "market_value": market_value,
                "day_pl": p.get("currentDayProfitLoss", 0) or 0,
                "day_pl_pct": p.get("currentDayProfitLossPercentage", 0) or 0,
                "open_pl": open_pl,
                "open_pl_pct": open_pl_pct,
                "expiration": _option_expiration(symbol),
                "strike": _option_strike(symbol),
                "category": None,
                "alloc_value": 0.0,
                "_asset": asset,
                "_long": long_qty,
                "_short": short_qty,
            }
        )

    # Equities and anything non-option resolve immediately.
    option_legs = []
    for leg in legs:
        if leg["_asset"] == "EQUITY":
            leg["category"] = "Stock"
            leg["alloc_value"] = leg["market_value"] or 0
        elif leg["_asset"] == "OPTION":
            option_legs.append(leg)
        else:
            leg["category"] = "Other"
            leg["alloc_value"] = abs(leg["market_value"] or 0)

    # Long shares held per ticker, used to identify covered calls. Consumed
    # as short calls are matched against them.
    cover_remaining: dict[str, int] = {}
    for leg in legs:
        if leg["_asset"] == "EQUITY":
            cover_remaining[leg["ticker"]] = (
                cover_remaining.get(leg["ticker"], 0) + (leg["_long"] or 0)
            )

    # Group options by underlying + type so spreads can be detected.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for leg in option_legs:
        pc = (leg["put_call"] or "").upper()
        kind = "PUT" if pc in ("PUT", "P") else "CALL" if pc in ("CALL", "C") else pc
        groups.setdefault((leg["ticker"], kind), []).append(leg)

    for (ticker, kind), group in groups.items():
        is_put = kind == "PUT"
        category = "Put spreads" if is_put else "Call spreads"
        unmatched_s = [l for l in group if (l["_short"] or 0) > 0]
        unmatched_l = [l for l in group if (l["_long"] or 0) > 0]

        # Pair legs into vertical spreads only: a short and a long sharing
        # an expiration. Legs at different expirations are left standalone
        # (no calendar/diagonal pairing).
        pairs = []
        for s in list(unmatched_s):
            for l in unmatched_l:
                if l["expiration"] == s["expiration"]:
                    pairs.append((s, l))
                    unmatched_s.remove(s)
                    unmatched_l.remove(l)
                    break

        # Value each vertical on its own legs.
        for s, l in pairs:
            qty = min(s["_short"] or 0, l["_long"] or 0)
            s_prem = abs(s["avg_price"] or 0)   # opening premiums, per share
            l_prem = abs(l["avg_price"] or 0)
            net_credit = s_prem - l_prem        # > 0 credit, < 0 debit
            if s["strike"] is not None and l["strike"] is not None:
                width = abs(s["strike"] - l["strike"])
            else:
                width = 0
            if net_credit >= 0:
                # Credit spread: defined risk = width minus credit received.
                alloc = max((width - net_credit) * 100 * qty, 0)
                carrier = s
            else:
                # Debit spread: net debit paid.
                alloc = (l_prem - s_prem) * 100 * qty
                carrier = l
            s["category"] = category
            l["category"] = category
            s["alloc_value"] = 0.0
            l["alloc_value"] = 0.0
            carrier["alloc_value"] = alloc

        # Standalone leftovers. Short calls covered by stock become covered
        # calls; everything else falls to the single-leg rules.
        for leg in unmatched_s + unmatched_l:
            short_qty = leg["_short"] or 0
            if not is_put and short_qty > 0:
                need = short_qty * 100
                if cover_remaining.get(ticker, 0) >= need:
                    cover_remaining[ticker] -= need
                    leg["category"] = "Covered calls"
                    leg["alloc_value"] = 0.0  # capital is in the underlying stock
                else:
                    leg["category"] = "Other"  # uncovered short call
                    leg["alloc_value"] = abs(leg["market_value"] or 0)
            else:
                leg["category"], leg["alloc_value"] = _classify_single_leg(leg)

    for leg in legs:
        for k in ("_asset", "_long", "_short"):
            leg.pop(k, None)
    return legs


def _underlying_symbol(instrument: dict[str, Any]) -> str:
    """Return the ticker a position rolls up under.

    For options this is the underlying (so AAPL calls and AAPL stock group
    together). Prefers Schwab's underlyingSymbol field, falling back to the
    leading letters of the OCC symbol.
    """
    asset = (instrument.get("assetType") or "").upper()
    if asset == "OPTION":
        underlying = instrument.get("underlyingSymbol")
        if underlying:
            return underlying
        match = re.match(r"^([A-Za-z.\/]+)", instrument.get("symbol", "").strip())
        if match:
            return match.group(1)
    return instrument.get("symbol", "")


def _option_strike(symbol: str) -> float | None:
    """Pull the strike out of an OCC-style option symbol.

    OCC symbols end in an 8-digit strike expressed in thousandths, e.g.
    'AAPL  240119C00150000' -> 150.0. Returns None if it can't parse.
    """
    match = re.search(r"(\d{8})$", symbol.strip())
    if not match:
        return None
    return int(match.group(1)) / 1000.0


def _option_expiration(symbol: str) -> str | None:
    """Pull the expiration date from an OCC-style option symbol.

    The 6 digits before the put/call letter are YYMMDD, e.g.
    'AAPL  240119C00150000' -> '2024-01-19'. Returns an ISO date string,
    or None for non-options or symbols it can't parse.
    """
    match = re.search(r"(\d{6})[CP]\d{8}$", symbol.replace(" ", ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%m%d").date().isoformat()
    except ValueError:
        return None


def get_recent_orders(
    c: client.Client, account_hash: str, days: int = 7
) -> list[dict[str, Any]]:
    """Return a flat list of recent orders for display."""
    now = datetime.now(timezone.utc)
    resp = c.get_orders_for_account(
        account_hash,
        from_entered_datetime=now - timedelta(days=days),
        to_entered_datetime=now,
    )
    resp.raise_for_status()
    return [_parse_order(o) for o in resp.json()]


def get_account_raw(c: client.Client, account_hash: str) -> dict[str, Any]:
    """Raw account JSON (balances + positions) straight from the API."""
    fields = client.Client.Account.Fields.POSITIONS
    resp = c.get_account(account_hash, fields=fields)
    resp.raise_for_status()
    return resp.json()


def get_orders_raw(
    c: client.Client, account_hash: str, days: int = 7
) -> list[dict[str, Any]]:
    """Raw orders JSON straight from the API."""
    now = datetime.now(timezone.utc)
    resp = c.get_orders_for_account(
        account_hash,
        from_entered_datetime=now - timedelta(days=days),
        to_entered_datetime=now,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_order(o: dict[str, Any]) -> dict[str, Any]:
    legs = o.get("orderLegCollection", [])
    first_leg = legs[0] if legs else {}
    instrument = first_leg.get("instrument", {})
    return {
        "entered": o.get("enteredTime", ""),
        "symbol": instrument.get("symbol", ""),
        "instruction": first_leg.get("instruction", ""),
        "type": o.get("orderType", ""),
        "quantity": o.get("quantity"),
        "filled": o.get("filledQuantity"),
        "status": o.get("status", ""),
    }


# ---------------------------------------------------------------------------
# AM-report helpers: raw index/futures quotes and option chains. All read-only.
# ---------------------------------------------------------------------------
def get_quotes_raw(c: "client.Client", symbols: list[str]) -> dict[str, dict]:
    """Full quote payloads (not just last price) for index/futures symbols like
    $VIX, $VIX3M, /ES, /NQ — the AM report needs netChange / closePrice too.
    Returns {symbol: quoteDict}; empty on error so one bad symbol can't break a run."""
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return {}
    try:
        resp = c.get_quotes(wanted)
        resp.raise_for_status()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for sym, payload in (resp.json() or {}).items():
        q = (payload or {}).get("quote", {}) or {}
        if q:
            out[sym] = q
    return out


def get_option_chain(c: "client.Client", symbol: str, days: int = 45,
                     strike_count: int = 50, puts_only: bool = False) -> dict | None:
    """Raw Schwab option chain (callExpDateMap / putExpDateMap) for `symbol`,
    covering expirations out to ~`days` DTE with ~`strike_count` strikes around
    the money. Carries per-contract delta/gamma/openInterest/bid/ask/mark/IV —
    the raw material for the 30-delta put pick and the gamma-wall math.

    `puts_only` fetches just the put side with a tighter strike count — used by the
    light intraday ladder refresh, which needs only put premiums (gamma walls run
    off end-of-day open interest and don't change during the session). Returns None
    on any error so one bad symbol can't break the run."""
    from datetime import date as _date, timedelta as _td
    ctype = c.Options.ContractType.PUT if puts_only else c.Options.ContractType.ALL
    try:
        resp = c.get_option_chain(
            symbol,
            contract_type=ctype,
            strike_count=strike_count,
            from_date=_date.today(),
            to_date=_date.today() + _td(days=days),
            include_underlying_quote=True,
        )
        resp.raise_for_status()
    except Exception:
        return None
    data = resp.json() or {}
    if not (data.get("callExpDateMap") or data.get("putExpDateMap")):
        return None
    return data
