"""
app.py
======

Streamlit dashboard for a Charles Schwab trading account (read-only).

Run with:
    streamlit run app.py

Prerequisite: a valid token created by `python auth_setup.py`.

Multi-page app. The sidebar nav switches between the Home overview, a
detail page per category (Stocks, CSPs, LEAPS, Other), and a Source data
page that shows the raw API responses.
"""

from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

import schwab_client as sc

st.set_page_config(page_title="Schwab Dashboard", page_icon=":material/finance:", layout="wide")

REFRESH_TTL = 60  # seconds; how long cached API data is reused

# Allocation targets as a percentage of liquidation value. Each entry is
# (label shown in the table, test that returns True when on target).
ALLOCATION_TARGETS = {
    "Stock": ("< 30%", lambda p: p < 30),
    "CSPs": ("> 55%", lambda p: p > 55),
    "LEAPS": ("<= 15%", lambda p: p <= 15),
}


def _highlight_off_target(row):
    """Amber background for any allocation row flagged Off target."""
    if row.get("Status") == "Off":
        return ["background-color: rgba(250, 199, 117, 0.45)"] * len(row)
    return [""] * len(row)


def _render_table(df, money_cols=(), pct_cols=(), price_cols=(), highlight=False):
    """Render a dataframe with comma-grouped, no-cents money columns,
    one-decimal percent columns, and two-decimal price columns, via a
    pandas Styler (NumberColumn printf formats can't do thousands
    separators). Tables size to their content."""
    fmt = {c: "${:,.0f}" for c in money_cols}
    fmt.update({c: "{:.1f}%" for c in pct_cols})
    fmt.update({c: "${:,.2f}" for c in price_cols})
    styler = df.style.format(fmt, na_rep="-")
    if highlight:
        styler = styler.apply(_highlight_off_target, axis=1)
    st.dataframe(styler, hide_index=True, width="content")


# --- Cached data access -------------------------------------------------
@st.cache_resource
def _client():
    return sc.get_client()


@st.cache_data(ttl=REFRESH_TTL)
def _accounts() -> list[dict]:
    return sc.list_accounts(_client())


@st.cache_data(ttl=REFRESH_TTL)
def _snapshot(account_hash: str) -> dict:
    return sc.get_account_snapshot(_client(), account_hash)


@st.cache_data(ttl=REFRESH_TTL)
def _account_raw(account_hash: str) -> dict:
    return sc.get_account_raw(_client(), account_hash)


@st.cache_data(ttl=REFRESH_TTL)
def _orders_raw(account_hash: str) -> list[dict]:
    return sc.get_orders_raw(_client(), account_hash)


def _money(value) -> str:
    if value is None:
        return "-"
    return f"${value:,.0f}"


def _get_snapshot():
    account_hash = st.session_state.get("account_hash")
    if not account_hash:
        st.info("Select an account in the sidebar.")
        return None
    try:
        return _snapshot(account_hash)
    except sc.AuthError as exc:
        st.error(str(exc))
        return None


# --- Home page ----------------------------------------------------------
def _vix_section(snap):
    vix = snap.get("vix")
    regime = snap.get("vix_regime")
    reco = snap.get("vix_reco")
    st.subheader("Volatility (VIX)")
    if vix is None:
        st.caption("VIX unavailable (needs the Market Data product enabled).")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("VIX", f"{vix:,.2f}", delta=regime, delta_color="off")
        if reco:
            m2.metric("Buying power", f"{reco['cash_pct']:.1f}%")
            m3.metric("Target", reco["target"])

    if reco:
        if reco["stance"] in ("deploy", "raise"):
            st.warning(reco["action"])
        else:
            st.success(reco["action"])
        if snap.get("options_bp_field"):
            st.caption(f"Buying power source: balances field "
                       f"'{snap['options_bp_field']}'. Reorder OPTIONS_BP_FIELDS "
                       f"in schwab_client.py if a different field is correct.")

    # Guide table; highlight the band the current VIX falls in.
    guide = pd.DataFrame(
        [
            {
                "VIX range": (f"{int(b['low'])}+" if b["high"] >= 1000
                              else f"<{int(b['high'])}" if b["low"] == 0
                              else f"{int(b['low'])}-{int(b['high'])}"),
                "Regime": b["regime"],
                "Cash": b["cash"],
                "Invested": b["invested"],
            }
            for b in sc.VIX_GUIDE
        ]
    )

    def _mark_active(row):
        active = vix is not None and sc.vix_regime(vix) == row["Regime"]
        return ["background-color: #fff3cd" if active else "" for _ in row]

    styler = guide.style.apply(_mark_active, axis=1)
    st.dataframe(styler, hide_index=True, width="content")
    st.caption("VIX Cash Allocation framework (Options Trading University). "
               "Cash percent is measured against liquidation value; the active "
               "band is highlighted.")


def _theta_section(snap):
    st.subheader("Theta (daily time decay)")
    total = snap.get("theta_total") or 0
    annual = snap.get("theta_annual_pct")
    by_ticker = snap.get("theta_by_ticker") or {}
    if not by_ticker:
        st.caption("No option theta available (needs the Market Data product "
                   "enabled).")
        return

    m1, m2 = st.columns(2)
    m1.metric("Theta / day", _money(total))
    m2.metric("Annualized", f"{annual:.1f}%" if annual is not None else "-")

    rows = [{"Ticker": t, "Theta / day": v}
            for t, v in sorted(by_ticker.items(), key=lambda kv: kv[1], reverse=True)]
    rows.append({"Ticker": "Total", "Theta / day": total})
    _render_table(pd.DataFrame(rows), money_cols=["Theta / day"])
    st.caption("Daily decay captured (shorts) net of decay paid (longs). "
               "Annualized is theta x 365 as a percent of liquidation value.")


def page_home():
    st.title("Account overview")
    snap = _get_snapshot()
    if snap is None:
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Liquidation value", _money(snap["liquidation_value"]))
    c2.metric("Cash", _money(snap["cash"]))
    c3.metric("Buying power", _money(snap["buying_power"]))
    c4.metric("Open P&L", _money(snap.get("open_pl")),
              delta=f"{(snap.get('open_pl') or 0):,.0f}")
    c5.metric("Day P&L", _money(snap["day_pl"]), delta=f"{snap['day_pl']:,.0f}")

    if snap.get("quotes_error"):
        st.warning(snap["quotes_error"])

    _vix_section(snap)
    _theta_section(snap)

    st.subheader("Portfolio allocation")
    allocation = snap.get("allocation", {})
    liq = snap.get("liquidation_value") or 0
    order = ["Stock", "Covered calls", "CSPs", "Put spreads", "Call spreads", "LEAPS", "Other"]
    rows = [(cat, allocation[cat]) for cat in order if allocation.get(cat)]
    rows += [(cat, val) for cat, val in allocation.items() if cat not in order and val]

    if not rows:
        st.caption("No positions to chart yet.")
        return

    alloc_df = pd.DataFrame(rows, columns=["Category", "Value"])
    position_total = alloc_df["Value"].sum()
    denom = liq if liq and liq > 0 else position_total
    alloc_df["Percent"] = alloc_df["Value"] / denom * 100
    alloc_df["PctLabel"] = alloc_df["Percent"].map(lambda v: f"{v:.1f}%")
    alloc_df["Slice"] = alloc_df["Category"] + " " + alloc_df["PctLabel"]
    alloc_df["Target"] = alloc_df["Category"].map(
        lambda c: ALLOCATION_TARGETS[c][0] if c in ALLOCATION_TARGETS else "-"
    )
    alloc_df["Status"] = alloc_df.apply(
        lambda r: (
            ""
            if r["Category"] not in ALLOCATION_TARGETS
            else ("On" if ALLOCATION_TARGETS[r["Category"]][1](r["Percent"]) else "Off")
        ),
        axis=1,
    )

    base = alt.Chart(alloc_df).encode(theta=alt.Theta("Value:Q", stack=True))
    arc = base.mark_arc(outerRadius=110).encode(
        color=alt.Color("Category:N", sort=order, legend=None),
        tooltip=[
            alt.Tooltip("Category:N"),
            alt.Tooltip("Value:Q", title="Value", format="$,.0f"),
            alt.Tooltip("PctLabel:N", title="Share"),
        ],
    )
    slice_labels = base.mark_text(radius=132, size=12).encode(text=alt.Text("Slice:N"))
    pie = (arc + slice_labels).properties(width=360, height=360)

    col_pie, col_tbl = st.columns([3, 2])
    with col_pie:
        st.altair_chart(pie)
    with col_tbl:
        alloc_view = alloc_df[["Category", "Value", "Percent", "Target", "Status"]]
        _render_table(alloc_view, money_cols=["Value"], pct_cols=["Percent"], highlight=True)
    st.caption(
        "Percentages are of liquidation value, so they will not total 100%. "
        "A total above 100% reflects allocation beyond liquidity. Stock and "
        "LEAPS use market value; CSPs use cash-secured collateral "
        "(strike x 100 x contracts). Off-target rows are highlighted."
    )

    margin_used = position_total - liq
    margin_df = pd.DataFrame(
        [
            ("Liquidation value", liq),
            ("Position value (allocation)", position_total),
            ("Margin in use", margin_used),
        ],
        columns=["Measure", "Amount"],
    )
    _render_table(margin_df, money_cols=["Amount"])
    if margin_used > 1:
        st.warning(
            f"Margin in use: {_money(margin_used)} "
            "(position value exceeds liquidation value)."
        )
    else:
        st.caption("No margin in use; position value is within liquidation value.")

    st.subheader("Positions by ticker")
    by_ticker = snap.get("by_ticker", {})
    if not by_ticker:
        st.caption("No positions to break out yet.")
        return

    tdf = pd.DataFrame(
        sorted(by_ticker.items(), key=lambda kv: kv[1], reverse=True),
        columns=["Ticker", "Value"],
    )
    tdf["Percent"] = tdf["Value"] / denom * 100
    tdf["PctLabel"] = tdf["Percent"].map(lambda v: f"{v:.1f}%")
    tdf["BarLabel"] = tdf["Ticker"] + "  " + tdf["PctLabel"]
    max_val = tdf["Value"].max()

    bar = alt.Chart(tdf).mark_bar().encode(
        x=alt.X("Value:Q", axis=None, scale=alt.Scale(domain=[0, max_val * 1.25])),
        y=alt.Y("Ticker:N", sort="-x", axis=None),
        tooltip=[
            alt.Tooltip("Ticker:N"),
            alt.Tooltip("Value:Q", title="Value", format="$,.0f"),
            alt.Tooltip("PctLabel:N", title="Share"),
        ],
    )
    bar_labels = alt.Chart(tdf).mark_text(align="left", dx=4, size=12).encode(
        x=alt.X("Value:Q"),
        y=alt.Y("Ticker:N", sort="-x"),
        text=alt.Text("BarLabel:N"),
    )
    ticker_chart = (bar + bar_labels).properties(width=420, height=max(140, 30 * len(tdf)))
    st.altair_chart(ticker_chart)
    prices = snap.get("underlying_prices", {})
    tdf["Price"] = tdf["Ticker"].map(prices.get)
    _render_table(
        tdf[["Ticker", "Price", "Value", "Percent"]],
        money_cols=["Value"], pct_cols=["Percent"], price_cols=["Price"],
    )


# --- Category detail pages ----------------------------------------------
def _category_charts(positions):
    """Pie + bar of this category's allocation value split by ticker."""
    agg: dict[str, float] = {}
    for p in positions:
        agg[p["ticker"]] = agg.get(p["ticker"], 0.0) + (p["alloc_value"] or 0)
    cdf = pd.DataFrame(
        sorted(agg.items(), key=lambda kv: kv[1], reverse=True),
        columns=["Ticker", "Value"],
    )
    total = cdf["Value"].sum()
    if cdf.empty or total <= 0:
        return
    cdf["Percent"] = cdf["Value"] / total * 100
    cdf["PctLabel"] = cdf["Percent"].map(lambda v: f"{v:.1f}%")
    cdf["Slice"] = cdf["Ticker"] + " " + cdf["PctLabel"]
    cdf["BarLabel"] = cdf["Ticker"] + "  " + cdf["PctLabel"]
    sort_order = list(cdf["Ticker"])
    tooltip = [
        alt.Tooltip("Ticker:N"),
        alt.Tooltip("Value:Q", title="Value", format="$,.0f"),
        alt.Tooltip("PctLabel:N", title="Share"),
    ]

    col_pie, col_bar = st.columns(2)
    with col_pie:
        base = alt.Chart(cdf).encode(theta=alt.Theta("Value:Q", stack=True))
        arc = base.mark_arc(outerRadius=100).encode(
            color=alt.Color("Ticker:N", sort=sort_order, legend=None),
            tooltip=tooltip,
        )
        slice_labels = base.mark_text(radius=120, size=11).encode(
            text=alt.Text("Slice:N")
        )
        st.altair_chart((arc + slice_labels).properties(width=320, height=320))
    with col_bar:
        max_val = cdf["Value"].max()
        bar = alt.Chart(cdf).mark_bar().encode(
            x=alt.X("Value:Q", axis=None, scale=alt.Scale(domain=[0, max_val * 1.25])),
            y=alt.Y("Ticker:N", sort="-x", axis=None),
            tooltip=tooltip,
        )
        bar_labels = alt.Chart(cdf).mark_text(align="left", dx=4, size=11).encode(
            x=alt.X("Value:Q"),
            y=alt.Y("Ticker:N", sort="-x"),
            text=alt.Text("BarLabel:N"),
        )
        st.altair_chart(
            (bar + bar_labels).properties(width=380, height=max(140, 30 * len(cdf)))
        )
    st.caption("Allocation value split by ticker within this category "
               "(share is of the category total).")


def _compact_money(v) -> str:
    if v is None:
        return ""
    a = abs(v)
    if a >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _csp_cash_plan(positions):
    """Time-phased view of when each block of CSP collateral resolves
    (freed if the put expires out of the money, or assigned into shares)."""
    st.subheader("Cash plan")
    plan: dict[str, dict] = {}
    missing = 0
    for p in positions:
        exp = p.get("expiration")
        if not exp:
            missing += 1
            continue
        e = plan.setdefault(exp, {"Collateral": 0.0, "Contracts": 0, "Tickers": set()})
        e["Collateral"] += p["alloc_value"] or 0
        e["Contracts"] += abs(p["quantity"] or 0)
        e["Tickers"].add(p["ticker"])

    if not plan:
        st.caption("No expiration dates available for these positions.")
        return

    today = dt.date.today()
    cum = 0.0
    data = []
    for exp, v in sorted(plan.items()):  # ISO strings sort chronologically
        cum += v["Collateral"]
        data.append(
            {
                "Expiration": exp,
                "Days out": (dt.date.fromisoformat(exp) - today).days,
                "Contracts": v["Contracts"],
                "Tickers": ", ".join(sorted(v["Tickers"])),
                "Collateral": v["Collateral"],
                "Cumulative": cum,
            }
        )
    pdf = pd.DataFrame(data)
    _render_table(pdf, money_cols=["Collateral", "Cumulative"])

    pdf["Label"] = pdf["Collateral"].map(_compact_money)
    order = list(pdf["Expiration"])
    bar = alt.Chart(pdf).mark_bar().encode(
        x=alt.X("Expiration:O", sort=order, title=None),
        y=alt.Y("Collateral:Q", title="Collateral resolving",
                axis=alt.Axis(format="$,.0f")),
        tooltip=[
            alt.Tooltip("Expiration:N"),
            alt.Tooltip("Collateral:Q", title="Collateral", format="$,.0f"),
            alt.Tooltip("Contracts:Q"),
            alt.Tooltip("Tickers:N"),
        ],
    )
    bar_labels = alt.Chart(pdf).mark_text(dy=-6, size=11).encode(
        x=alt.X("Expiration:O", sort=order),
        y=alt.Y("Collateral:Q"),
        text=alt.Text("Label:N"),
    )
    st.altair_chart((bar + bar_labels).properties(height=320))
    if missing:
        st.caption(f"{missing} position(s) had no readable expiration and were omitted.")
    st.caption(
        "At each expiration the cash-secured collateral resolves: freed if the "
        "put expires out of the money, or converted to shares if assigned. "
        "Cumulative is the running total of collateral resolving by that date."
    )


def _category_page(title: str, category: str, is_option: bool, ticker_focused: bool,
                   cash_plan: bool = False):
    st.title(title)
    snap = _get_snapshot()
    if snap is None:
        return

    positions = [p for p in snap["positions"] if p.get("category") == category]
    if not positions:
        st.caption(f"No {title} positions in this account.")
        return

    liq = snap.get("liquidation_value") or 0
    alloc_total = sum(p["alloc_value"] for p in positions)
    day_total = sum(p["day_pl"] for p in positions)
    share = (alloc_total / liq * 100) if liq else 0

    m1, m2, m3, m4 = st.columns(4)
    if ticker_focused:
        m1.metric("Tickers", len({p["ticker"] for p in positions}))
    else:
        m1.metric("Positions", len(positions))
    m2.metric("Allocation value", _money(alloc_total))
    m3.metric("Share of liq value", f"{share:.1f}%")
    m4.metric("Day P&L", _money(day_total), delta=f"{day_total:,.0f}")

    st.subheader("Breakdown by ticker")
    _category_charts(positions)

    if ticker_focused:
        agg: dict[str, dict] = {}
        for p in positions:
            a = agg.setdefault(
                p["ticker"], {"Price": p.get("underlying_price"), "Contracts": 0,
                              "Premium": 0.0, "Alloc value": 0.0, "Market value": 0.0,
                              "Theta": 0.0, "Open P&L": 0.0, "Day P&L": 0.0}
            )
            a["Contracts"] += abs(p["quantity"] or 0)
            a["Premium"] += p.get("premium") or 0
            a["Alloc value"] += p["alloc_value"] or 0
            a["Market value"] += p["market_value"] or 0
            a["Theta"] += p.get("theta_dollars") or 0
            a["Open P&L"] += p["open_pl"] or 0
            a["Day P&L"] += p["day_pl"] or 0
        ranked = sorted(agg.items(), key=lambda kv: kv[1]["Alloc value"], reverse=True)
        df = pd.DataFrame(
            [
                {
                    "Ticker": t,
                    "Price": v["Price"],
                    "Contracts": v["Contracts"],
                    "Premium": v["Premium"],
                    "Alloc value": v["Alloc value"],
                    "% of liq": (v["Alloc value"] / liq * 100) if liq else 0,
                    "Market value": v["Market value"],
                    "Theta": v["Theta"],
                    "Open P&L": v["Open P&L"],
                    "Day P&L": v["Day P&L"],
                }
                for t, v in ranked
            ]
        )
        _render_table(
            df,
            money_cols=["Premium", "Alloc value", "Market value", "Theta",
                        "Open P&L", "Day P&L"],
            pct_cols=["% of liq"],
            price_cols=["Price"],
        )
        st.caption("Aggregated by ticker. Price is the underlying's current "
                   "market price. Contracts is the total number of option "
                   "contracts for that underlying in this category.")
    else:
        cols = ["symbol", "ticker", "underlying_price"]
        if is_option:
            cols += ["put_call", "expiration", "strike", "dte"]
        cols += ["quantity", "avg_price"]
        if is_option:
            cols += ["premium"]
        cols += ["market_value", "day_pl", "day_pl_pct", "open_pl", "open_pl_pct",
                 "alloc_value"]
        if is_option:
            cols += ["theta_dollars"]
        rename = {
            "symbol": "Symbol", "ticker": "Ticker", "underlying_price": "Price",
            "put_call": "P/C",
            "expiration": "Expiration", "strike": "Strike", "dte": "DTE",
            "quantity": "Qty",
            "avg_price": "Avg price", "premium": "Premium",
            "market_value": "Market value", "day_pl": "Day P&L",
            "day_pl_pct": "Day P&L %", "open_pl": "Open P&L", "open_pl_pct": "Open P&L %",
            "alloc_value": "Alloc value", "theta_dollars": "Theta",
        }
        df = pd.DataFrame(positions)[cols].rename(columns=rename)
        money_cols = ["Avg price", "Market value", "Day P&L", "Open P&L", "Alloc value"]
        if is_option:
            money_cols += ["Strike", "Premium", "Theta"]
        _render_table(df, money_cols=money_cols, pct_cols=["Day P&L %", "Open P&L %"],
                      price_cols=["Price"])
        st.caption("Market value is the position's own value; Alloc value is the "
                   "capital-committed basis used on the Home allocation chart. For "
                   "spreads, collateral is shown on the short leg.")

    if category == "CSPs":
        st.subheader("Capital efficiency by contract")
        rows = []
        for p in sorted(positions, key=lambda x: (x.get("capital_efficiency") or -1),
                        reverse=True):
            contracts = abs(p.get("quantity") or 0)
            mv = abs(p.get("market_value") or 0)
            put_price = (mv / (100 * contracts)) if contracts else None
            price = p.get("underlying_price")
            strike = p.get("strike")
            to_strike = ((price - strike) / price * 100) if (price and strike) else None
            rows.append({
                "Ticker": p["ticker"],
                "Price": price,
                "Strike": strike,
                "To Strike": to_strike,
                "Expiration": p.get("expiration"),
                "DTE": p.get("dte"),
                "Qty": contracts,
                "Premium": p.get("premium"),
                "Put price": put_price,
                "Collateral": p.get("alloc_value"),
                "Capital efficiency": p.get("capital_efficiency"),
            })
        _render_table(
            pd.DataFrame(rows),
            money_cols=["Collateral", "Premium"],
            pct_cols=["Capital efficiency", "To Strike"],
            price_cols=["Put price", "Strike", "Price"],
        )
        st.caption(
            "To Strike is how far the underlying sits above the strike as a "
            "percent of its price: positive means the put is out of the money "
            "(that much cushion before assignment), negative means in the "
            "money. Capital efficiency is the annualized return on collateral: "
            "(360 / DTE) x current put value / collateral; a 30-day put worth "
            "about 3.5% of its collateral scores about 42%. Blank where a price, "
            "DTE, or collateral is unavailable."
        )

    if cash_plan:
        _csp_cash_plan(positions)


def page_stocks():
    _category_page("Stocks", "Stock", is_option=False, ticker_focused=False)


def page_csps():
    _category_page("CSPs", "CSPs", is_option=True, ticker_focused=True, cash_plan=True)


def page_covered_calls():
    _category_page("Covered calls", "Covered calls", is_option=True,
                   ticker_focused=False)


def page_put_spreads():
    _category_page("Put spreads", "Put spreads", is_option=True, ticker_focused=False)


def page_call_spreads():
    _category_page("Call spreads", "Call spreads", is_option=True, ticker_focused=False)


def page_leaps():
    _category_page("LEAPS", "LEAPS", is_option=True, ticker_focused=True)


def page_other():
    _category_page("Other", "Other", is_option=True, ticker_focused=False)


# --- Source data page ---------------------------------------------------
def page_source():
    st.title("Source data")
    st.caption("Raw responses pulled from the Schwab API for this account.")
    account_hash = st.session_state.get("account_hash")
    if not account_hash:
        st.info("Select an account in the sidebar.")
        return
    try:
        st.subheader("Linked accounts")
        st.json(_accounts())
        st.subheader("Account (balances + positions)")
        st.json(_account_raw(account_hash))
        st.subheader("Recent orders (7 days)")
        st.json(_orders_raw(account_hash))
    except sc.AuthError as exc:
        st.error(str(exc))


# --- Entry point: auth, account picker, navigation ----------------------
def main():
    try:
        accounts = _accounts()
    except sc.AuthError as exc:
        st.error(str(exc))
        st.info("Once you have re-authenticated, click below.")
        if st.button("Reload"):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        return

    if not accounts:
        st.warning("No linked accounts found for this app.")
        return

    pages = [
        st.Page(page_home, title="Home", icon=":material/home:", default=True),
        st.Page(page_stocks, title="Stocks", icon=":material/show_chart:"),
        st.Page(page_csps, title="CSPs", icon=":material/trending_down:"),
        st.Page(page_covered_calls, title="Covered calls",
                icon=":material/shield:"),
        st.Page(page_put_spreads, title="Put spreads", icon=":material/call_split:"),
        st.Page(page_call_spreads, title="Call spreads", icon=":material/call_merge:"),
        st.Page(page_leaps, title="LEAPS", icon=":material/trending_up:"),
        st.Page(page_other, title="Other", icon=":material/category:"),
        st.Page(page_source, title="Source data", icon=":material/database:"),
    ]
    pg = st.navigation(pages)

    with st.sidebar:
        st.divider()
        st.subheader("Account")
        labels = [a["number"] for a in accounts]
        choice = st.selectbox("Account", labels, key="account_choice",
                              label_visibility="collapsed")
        st.session_state["account_hash"] = next(
            a["hash"] for a in accounts if a["number"] == choice
        )
        if st.button("Refresh now"):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"Auto-refreshes every {REFRESH_TTL}s.")

    pg.run()


if __name__ == "__main__":
    main()
