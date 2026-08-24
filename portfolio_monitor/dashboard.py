"""Bloomberg-terminal-style single-page Streamlit dashboard.

Pure view layer: reads whatever `runner.py` last wrote to SQLite. Makes no
yfinance or Claude API calls itself, so it's free and instant to open --
run `runner.py` (or one of the earlier layer CLIs) first to populate data.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# `streamlit run` executes this file standalone, not as part of the
# portfolio_monitor package, so make the package importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portfolio_monitor import db as db_mod
from portfolio_monitor.greeks import bs_price, intrinsic_value

st.set_page_config(page_title="Portfolio Monitor", layout="wide", initial_sidebar_state="expanded")

TERMINAL_CSS = """
<style>
:root {
    --bg: #0a0e14;
    --panel: #11161f;
    --border: #232a36;
    --text: #d7dde5;
    --muted: #7a8699;
    --amber: #ffb000;
    --green: #3ddc84;
    --red: #ff5c5c;
    --yellow: #ffd60a;
    --blue: #5ac8fa;
}
html, body, [class*="css"] {
    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace !important;
}
.stApp { background-color: var(--bg); }
[data-testid="stMetricValue"] { font-family: 'SFMono-Regular', Consolas, monospace; }
.pm-panel {
    background-color: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 1rem;
    margin-bottom: 0.5rem;
}
.pm-alert {
    border-left: 4px solid var(--muted);
    padding: 0.35rem 0.6rem;
    margin-bottom: 0.35rem;
    background-color: var(--panel);
    font-size: 0.85rem;
}
.pm-alert.high { border-left-color: var(--red); }
.pm-alert.warn { border-left-color: var(--yellow); }
.pm-alert.info { border-left-color: var(--blue); }
.pm-alert.newstrike { border-left-color: var(--yellow); background-color: #1f1a08; }
.pm-badge {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 0.7rem; font-weight: 600; margin-left: 6px;
}
.pm-badge.new { background-color: var(--yellow); color: #000; }
.pm-badge.held { background-color: var(--amber); color: #000; }
h1, h2, h3 { font-family: 'SFMono-Regular', Consolas, monospace !important; }
</style>
"""
st.markdown(TERMINAL_CSS, unsafe_allow_html=True)

SEVERITY_COLOR = {"high": "var(--red)", "warn": "var(--yellow)", "info": "var(--blue)"}


# --- Data loading --------------------------------------------------------------

@st.cache_resource
def get_conn(db_path: str):
    return db_mod.connect(db_path)


def load(db_path: str):
    conn = get_conn(db_path)
    asof_dates = db_mod.list_asof_dates(conn)
    return conn, asof_dates


# --- Sections --------------------------------------------------------------

def render_status_strip(conn, asof: str, positions: list[dict], valuations: dict[str, dict]) -> list[dict]:
    total_value = sum(v["current_value"] for v in valuations.values() if v["current_value"] is not None)
    total_pnl = sum(v["pnl_dollar"] for v in valuations.values() if v["pnl_dollar"] is not None)
    total_cost = total_value - total_pnl
    pnl_pct = (total_pnl / total_cost * 100.0) if total_cost else 0.0

    alerts = db_mod.get_alerts(conn, asof)
    greeks = db_mod.get_portfolio_greeks(conn, asof) or {}
    macro = db_mod.get_macro_gate(conn, asof) or {}

    cols = st.columns(8)
    cols[0].metric("NAV", f"${total_value:,.0f}")
    cols[1].metric("P&L", f"${total_pnl:,.0f}", f"{pnl_pct:.1f}%")
    cols[2].metric("Positions", str(len(positions)))
    cols[3].metric("Alerts", str(len(alerts)))
    cols[4].metric("Macro score", f"{macro.get('score', 0):.0f}/100" if macro else "n/a")
    cols[5].metric("Net delta", f"{greeks.get('net_delta_shares', 0):,.0f} sh" if greeks else "n/a")
    cols[6].metric("Daily theta", f"${greeks.get('daily_theta_dollars', 0):,.0f}/d" if greeks else "n/a")
    cols[7].metric("As of", asof)
    return alerts


def render_alerts_panel(alerts: list[dict]) -> None:
    st.subheader("Active Alerts")
    if not alerts:
        st.markdown('<div class="pm-panel">No active alerts.</div>', unsafe_allow_html=True)
        return
    html = []
    for a in alerts:
        css_class = a["severity"]
        if a["alert_type"] in ("new_strikes", "new_expiry"):
            css_class += " newstrike"
        html.append(
            f'<div class="pm-alert {css_class}"><b>[{a["severity"].upper()}]</b> '
            f'{a["alert_type"]} '
            f'{"(" + a["ticker"] + ")" if a["ticker"] else ""} &mdash; {a["message"]}</div>'
        )
    st.markdown("".join(html), unsafe_allow_html=True)


def render_positions_grid(positions: list[dict], valuations: dict[str, dict]) -> None:
    st.subheader("Positions")
    rows = []
    for p in positions:
        v = valuations.get(p["id"], {})
        is_option = p["asset_type"] == "option"
        dte = v.get("dte")
        if is_option and dte is not None:
            dte_badge = ("\U0001F534" if dte <= 15 else "\U0001F7E1" if dte <= 45 else "\U0001F7E2") + f" {dte}d"
        else:
            dte_badge = "-"

        contracts = p["contracts"]
        mult = 100 if is_option else 1
        delta_dollar = (v.get("delta") or 0) * contracts * mult if is_option else contracts
        theta_dollar = (v.get("theta") or 0) * contracts * mult if is_option else 0.0
        vega_dollar = (v.get("vega") or 0) * contracts * mult if is_option else 0.0

        rows.append({
            "ID": p["id"],
            "Ticker": p["ticker"],
            "Type": p["option_type"] or "shares",
            "Strike": p["strike"],
            "Expiry": p["expiry"] or "-",
            "DTE": dte_badge,
            "Entry": p["entry_price"],
            "Mark": v.get("mark"),
            "Value": v.get("current_value"),
            "P&L $": v.get("pnl_dollar"),
            "P&L %": v.get("pnl_pct"),
            "Delta ($/sh)": delta_dollar,
            "Theta $/d": theta_dollar,
            "Vega $": vega_dollar,
            "IV": (v["iv"] * 100) if v.get("iv") is not None else None,
            "->Target": max(0.0, min(v["progress_to_target_pct"], 100.0)) if v.get("progress_to_target_pct") is not None else None,
            "->Stop": max(0.0, min(v["progress_to_stop_pct"], 100.0)) if v.get("progress_to_stop_pct") is not None else None,
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        width='stretch',
        column_config={
            "Strike": st.column_config.NumberColumn(format="$%.2f"),
            "Entry": st.column_config.NumberColumn(format="$%.2f"),
            "Mark": st.column_config.NumberColumn(format="$%.2f"),
            "Value": st.column_config.NumberColumn(format="$%.0f"),
            "P&L $": st.column_config.NumberColumn(format="$%.0f"),
            "P&L %": st.column_config.NumberColumn(format="%.1f%%"),
            "Delta ($/sh)": st.column_config.NumberColumn(format="%.1f"),
            "Theta $/d": st.column_config.NumberColumn(format="$%.2f"),
            "Vega $": st.column_config.NumberColumn(format="$%.2f"),
            "IV": st.column_config.NumberColumn(format="%.1f%%"),
            "->Target": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            "->Stop": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
        },
    )


def render_theta_decay(conn, asof: str, positions: list[dict], valuations: dict[str, dict],
                        risk_free_rate: float = 0.045) -> None:
    st.subheader("Theta Decay (illustrative -- BS at constant spot + IV, not predictive)")
    options = [p for p in positions if p["asset_type"] == "option"]
    if not options:
        st.caption("No option positions.")
        return

    asof_dt = datetime.strptime(asof, "%Y-%m-%d").date()
    spots: dict[str, float] = {}
    for ticker in {p["ticker"] for p in options}:
        row = conn.execute("SELECT spot FROM underlying_quotes WHERE ticker = ? AND asof_date = ?",
                            (ticker, asof)).fetchone()
        spots[ticker] = row[0] if row else None

    max_expiry = max(datetime.strptime(p["expiry"], "%Y-%m-%d").date() for p in options)
    horizon_days = (max_expiry - asof_dt).days

    col1, col2 = st.columns(2)

    # (a) aggregate $ value projection stacked by position, held flat at intrinsic after own expiry
    fig1 = go.Figure()
    dates = [asof_dt + timedelta(days=d) for d in range(0, horizon_days + 1)]
    for p in options:
        v = valuations.get(p["id"], {})
        spot = spots.get(p["ticker"])
        iv = v.get("iv")
        expiry_dt = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
        if spot is None or iv is None:
            continue
        ys = []
        for d in dates:
            dte = (expiry_dt - d).days
            price = bs_price(spot, p["strike"], max(dte, 0), risk_free_rate, iv, p["option_type"])
            ys.append((price or 0.0) * 100 * p["contracts"])
        fig1.add_trace(go.Scatter(x=dates, y=ys, name=p["id"], stackgroup="one", mode="lines"))
    fig1.update_layout(template="plotly_dark", height=340, margin=dict(l=10, r=10, t=30, b=10),
                        title="Aggregate $ Value Projection", yaxis_title="$ value")
    col1.plotly_chart(fig1, width='stretch')

    # (b) extrinsic value per share decay curve, per option, to its own expiry
    fig2 = go.Figure()
    for p in options:
        v = valuations.get(p["id"], {})
        spot = spots.get(p["ticker"])
        iv = v.get("iv")
        expiry_dt = datetime.strptime(p["expiry"], "%Y-%m-%d").date()
        if spot is None or iv is None:
            continue
        own_dates = [asof_dt + timedelta(days=d) for d in range(0, (expiry_dt - asof_dt).days + 1)]
        intrinsic = intrinsic_value(spot, p["strike"], p["option_type"])
        ys = []
        for d in own_dates:
            dte = (expiry_dt - d).days
            price = bs_price(spot, p["strike"], max(dte, 0), risk_free_rate, iv, p["option_type"])
            ys.append((price or 0.0) - intrinsic)
        fig2.add_trace(go.Scatter(x=own_dates, y=ys, name=p["id"], mode="lines"))
    fig2.update_layout(template="plotly_dark", height=340, margin=dict(l=10, r=10, t=30, b=10),
                        title="Extrinsic Value / Share Decay Curve", yaxis_title="$/share")
    col2.plotly_chart(fig2, width='stretch')


def render_strike_ladder(conn, asof: str, positions: list[dict]) -> None:
    st.subheader("Strike Ladder")
    options = [p for p in positions if p["asset_type"] == "option"]
    if not options:
        st.caption("No option positions.")
        return

    new_strike_keys = {
        (d["ticker"], d["expiry"], d["option_type"], d["strike"])
        for d in db_mod.get_chain_diffs(conn, asof) if d["kind"] == "new_strike"
    }

    for p in options:
        rows = db_mod.get_chain_rows(conn, p["ticker"], p["expiry"], p["option_type"], asof)
        if not rows:
            st.caption(f"{p['id']}: no chain snapshot for {asof}")
            continue
        strikes = [r["strike"] for r in rows]
        try:
            idx = strikes.index(p["strike"])
        except ValueError:
            idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - p["strike"]))
        lo, hi = max(0, idx - 6), min(len(rows), idx + 7)
        window = rows[lo:hi]

        table_rows = []
        for r in window:
            badges = []
            if r["strike"] == p["strike"]:
                badges.append('<span class="pm-badge held">HELD</span>')
            if (p["ticker"], p["expiry"], p["option_type"], r["strike"]) in new_strike_keys:
                badges.append('<span class="pm-badge new">NEW</span>')
            table_rows.append({
                "Strike": f"${r['strike']:g}" + "".join(badges),
                "Bid": r["bid"], "Ask": r["ask"], "Last": r["last"],
                "IV": f"{r['iv']*100:.1f}%" if r["iv"] is not None else "-",
                "Vol": r["volume"], "OI": r["open_interest"],
            })

        st.markdown(f"**{p['id']}**  ({p['option_type']}, expiry {p['expiry']})", unsafe_allow_html=True)
        df = pd.DataFrame(table_rows)
        st.write(df.to_html(escape=False, index=False), unsafe_allow_html=True)


def render_allocation(conn, asof: str) -> None:
    st.subheader("Allocation")
    rows = db_mod.get_allocation(conn, asof)
    ticker_rows = [r for r in rows if r["group_type"] == "ticker"]
    sector_rows = [r for r in rows if r["group_type"] == "sector"]

    col1, col2 = st.columns(2)
    for col, group_rows, title in ((col1, ticker_rows, "By Ticker"), (col2, sector_rows, "By Sector")):
        if not group_rows:
            col.caption(f"{title}: no data")
            continue
        colors = ["#ff5c5c" if r["flagged"] else "#5ac8fa" for r in group_rows]
        fig = go.Figure(go.Bar(
            x=[r["pct"] for r in group_rows], y=[r["group_key"] for r in group_rows],
            orientation="h", marker_color=colors,
        ))
        cap = group_rows[0]["cap_pct"]
        fig.add_vline(x=cap, line_dash="dash", line_color="#ffb000", annotation_text=f"cap {cap:.0f}%")
        fig.update_layout(template="plotly_dark", height=260, margin=dict(l=10, r=10, t=30, b=10), title=title)
        col.plotly_chart(fig, width='stretch')


def render_news(conn, asof: str) -> None:
    st.subheader("News")
    rows = db_mod.get_news(conn, asof)
    if not rows:
        st.caption("No news analysis for this date.")
        return
    sentiment_color = {"positive": "var(--green)", "neutral": "var(--muted)", "negative": "var(--red)"}
    for r in rows:
        color = sentiment_color.get(r["sentiment"], "var(--muted)")
        with st.expander(f"{r['ticker']}  —  {(r['sentiment'] or 'n/a').upper()}  ({r['headline_count']} headlines)"):
            st.markdown(f'<span style="color:{color}">&#9679;</span> {r["summary"] or "n/a"}', unsafe_allow_html=True)
            drivers = json.loads(r["key_drivers"]) if r["key_drivers"] else []
            if drivers:
                st.markdown("**Drivers:** " + "; ".join(drivers))
            if r["position_flag"]:
                st.markdown(f'<div class="pm-alert warn"><b>Position flag:</b> {r["position_flag"]}</div>',
                             unsafe_allow_html=True)
            headlines = json.loads(r["headlines_json"]) if r["headlines_json"] else []
            for h in headlines:
                st.markdown(f"- [{h['title']}]({h['url']})  <span style='color:var(--muted)'>({h['publisher']})</span>",
                             unsafe_allow_html=True)


# --- Main --------------------------------------------------------------

def main() -> None:
    st.sidebar.title("Portfolio Monitor")
    default_db = os.environ.get("PORTFOLIO_DB_PATH", "portfolio.db")
    db_path = st.sidebar.text_input("Database path", value=default_db)

    conn, asof_dates = load(db_path)
    if not asof_dates:
        st.warning(f"No data found in {db_path}. Run `python -m portfolio_monitor.runner` first.")
        return

    asof = st.sidebar.selectbox("As-of date", asof_dates, index=0)
    if st.sidebar.button("Refresh"):
        st.cache_resource.clear()
        st.rerun()

    positions = db_mod.get_positions(conn)
    val_rows = db_mod.get_valuations(conn, asof)
    valuations = {v["position_id"]: v for v in val_rows}

    st.title("Portfolio Monitor")
    alerts = render_status_strip(conn, asof, positions, valuations)
    render_alerts_panel(alerts)
    render_positions_grid(positions, valuations)
    render_theta_decay(conn, asof, positions, valuations)
    render_strike_ladder(conn, asof, positions)
    render_allocation(conn, asof)
    render_news(conn, asof)


if __name__ == "__main__":
    main()
