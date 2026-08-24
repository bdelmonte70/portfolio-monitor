"""SQLite storage: positions, chain snapshots, underlying quotes, and valuations."""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    ticker TEXT NOT NULL,
    option_type TEXT,
    strike REAL,
    expiry TEXT,
    entry_price REAL NOT NULL,
    contracts REAL NOT NULL,
    entry_date TEXT NOT NULL,
    target_price REAL,
    stop_price REAL
);

CREATE TABLE IF NOT EXISTS chain_snapshots (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    expiry TEXT NOT NULL,
    option_type TEXT NOT NULL,
    strike REAL NOT NULL,
    bid REAL,
    ask REAL,
    last REAL,
    iv REAL,
    volume INTEGER,
    open_interest INTEGER,
    PRIMARY KEY (ticker, asof_date, expiry, option_type, strike)
);

CREATE TABLE IF NOT EXISTS underlying_quotes (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    spot REAL,
    PRIMARY KEY (ticker, asof_date)
);

CREATE TABLE IF NOT EXISTS valuations (
    position_id TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    mark REAL,
    current_value REAL,
    pnl_dollar REAL,
    pnl_pct REAL,
    dte INTEGER,
    progress_to_target_pct REAL,
    progress_to_stop_pct REAL,
    iv REAL,
    delta REAL,
    gamma REAL,
    theta REAL,
    vega REAL,
    PRIMARY KEY (position_id, asof_date),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

-- Layer 2: portfolio analytics

CREATE TABLE IF NOT EXISTS sector_map (
    ticker TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocation (
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    group_type TEXT NOT NULL,   -- 'ticker' | 'sector'
    group_key TEXT NOT NULL,
    value REAL NOT NULL,
    pct REAL NOT NULL,
    cap_pct REAL NOT NULL,
    flagged INTEGER NOT NULL,  -- 0/1
    PRIMARY KEY (asof_date, group_type, group_key)
);

CREATE TABLE IF NOT EXISTS portfolio_greeks (
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    net_delta_shares REAL,
    daily_theta_dollars REAL,
    net_vega_dollars REAL,
    PRIMARY KEY (asof_date)
);

CREATE TABLE IF NOT EXISTS iv_environment (
    position_id TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    current_iv REAL,
    iv_rank REAL,
    iv_percentile REAL,
    history_days INTEGER NOT NULL,
    status TEXT NOT NULL,       -- 'building_history' | 'ready'
    iv_flag TEXT,               -- 'rich' | 'cheap' | 'normal' | NULL
    dte INTEGER,
    near_expiry INTEGER,        -- 0/1, NULL for shares
    PRIMARY KEY (position_id, asof_date),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

-- Layer 3: market context

CREATE TABLE IF NOT EXISTS macro_gate (
    asof_date TEXT PRIMARY KEY,
    run_timestamp TEXT NOT NULL,
    score REAL,
    vix_level REAL,
    vix_percentile REAL,
    vix_score REAL,
    vix3m_level REAL,
    term_ratio REAL,
    term_percentile REAL,
    term_score REAL,
    breadth_pct REAL,
    breadth_score REAL,
    credit_ratio REAL,
    credit_percentile REAL,
    credit_score REAL,
    weight_vix REAL,
    weight_term_structure REAL,
    weight_breadth REAL,
    weight_credit REAL
);

CREATE TABLE IF NOT EXISTS news_analysis (
    ticker TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    headline_count INTEGER NOT NULL,
    summary TEXT,
    sentiment TEXT,
    key_drivers TEXT,   -- JSON array of strings
    position_flag TEXT,
    model TEXT,
    headlines_json TEXT,  -- JSON array of {title, url, publisher, published}
    PRIMARY KEY (ticker, asof_date)
);

-- Layer 4: diff, alerts, notifier

CREATE TABLE IF NOT EXISTS chain_diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'new_expiry' | 'new_strike'
    expiry TEXT NOT NULL,
    option_type TEXT,            -- NULL for new_expiry
    strike REAL                  -- NULL for new_expiry
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date TEXT NOT NULL,
    run_timestamp TEXT NOT NULL,
    alert_type TEXT NOT NULL,    -- target_hit | stop_hit | target_near | iv_change | new_strikes | new_expiry | news_flag
    severity TEXT NOT NULL,      -- high | warn | info
    ticker TEXT,
    position_id TEXT,
    message TEXT NOT NULL,
    detail TEXT                  -- JSON blob with extra structured fields
);
"""

# Additive columns for tables created by earlier layers, applied to existing
# databases that predate them (fresh databases already have them via SCHEMA).
_MIGRATIONS = [
    ("news_analysis", "headlines_json", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, coltype in _MIGRATIONS:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: callers that cache a connection across calls
    # (e.g. Streamlit's st.cache_resource, which can rerun scripts on a
    # different thread) need to reuse it safely. All writes in this codebase
    # go through a single connection per run, so this doesn't introduce
    # concurrent-write risk -- it only relaxes sqlite3's same-thread check.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def upsert_positions(conn: sqlite3.Connection, positions: Iterable) -> None:
    rows = []
    for p in positions:
        if p.asset_type == "option":
            rows.append((p.id, p.asset_type, p.ticker, p.option_type, p.strike, p.expiry,
                         p.entry_price, p.contracts, p.entry_date, p.target_price, p.stop_price))
        else:
            rows.append((p.id, p.asset_type, p.ticker, None, None, None,
                         p.entry_price, p.contracts, p.entry_date, p.target_price, p.stop_price))

    conn.executemany(
        """
        INSERT INTO positions (id, asset_type, ticker, option_type, strike, expiry,
                                entry_price, contracts, entry_date, target_price, stop_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            asset_type=excluded.asset_type, ticker=excluded.ticker, option_type=excluded.option_type,
            strike=excluded.strike, expiry=excluded.expiry, entry_price=excluded.entry_price,
            contracts=excluded.contracts, entry_date=excluded.entry_date,
            target_price=excluded.target_price, stop_price=excluded.stop_price
        """,
        rows,
    )
    conn.commit()


def upsert_underlying_quote(conn: sqlite3.Connection, ticker: str, asof_date: str,
                             run_timestamp: str, spot: float | None) -> None:
    conn.execute(
        """
        INSERT INTO underlying_quotes (ticker, asof_date, run_timestamp, spot)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, spot=excluded.spot
        """,
        (ticker, asof_date, run_timestamp, spot),
    )
    conn.commit()


def upsert_chain_snapshot_rows(conn: sqlite3.Connection, ticker: str, asof_date: str,
                                run_timestamp: str, rows: Iterable[dict]) -> None:
    values = [
        (ticker, asof_date, run_timestamp, r["expiry"], r["option_type"], r["strike"],
         r.get("bid"), r.get("ask"), r.get("last"), r.get("iv"), r.get("volume"), r.get("open_interest"))
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO chain_snapshots (ticker, asof_date, run_timestamp, expiry, option_type, strike,
                                      bid, ask, last, iv, volume, open_interest)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date, expiry, option_type, strike) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, bid=excluded.bid, ask=excluded.ask,
            last=excluded.last, iv=excluded.iv, volume=excluded.volume, open_interest=excluded.open_interest
        """,
        values,
    )
    conn.commit()


def upsert_allocation_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO allocation (asof_date, run_timestamp, group_type, group_key, value, pct, cap_pct, flagged)
        VALUES (:asof_date, :run_timestamp, :group_type, :group_key, :value, :pct, :cap_pct, :flagged)
        ON CONFLICT(asof_date, group_type, group_key) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, value=excluded.value, pct=excluded.pct,
            cap_pct=excluded.cap_pct, flagged=excluded.flagged
        """,
        list(rows),
    )
    conn.commit()


def upsert_portfolio_greeks(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO portfolio_greeks (asof_date, run_timestamp, net_delta_shares, daily_theta_dollars, net_vega_dollars)
        VALUES (:asof_date, :run_timestamp, :net_delta_shares, :daily_theta_dollars, :net_vega_dollars)
        ON CONFLICT(asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, net_delta_shares=excluded.net_delta_shares,
            daily_theta_dollars=excluded.daily_theta_dollars, net_vega_dollars=excluded.net_vega_dollars
        """,
        row,
    )
    conn.commit()


def upsert_iv_environment_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO iv_environment (position_id, asof_date, run_timestamp, current_iv, iv_rank, iv_percentile,
                                     history_days, status, iv_flag, dte, near_expiry)
        VALUES (:position_id, :asof_date, :run_timestamp, :current_iv, :iv_rank, :iv_percentile,
                :history_days, :status, :iv_flag, :dte, :near_expiry)
        ON CONFLICT(position_id, asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, current_iv=excluded.current_iv, iv_rank=excluded.iv_rank,
            iv_percentile=excluded.iv_percentile, history_days=excluded.history_days, status=excluded.status,
            iv_flag=excluded.iv_flag, dte=excluded.dte, near_expiry=excluded.near_expiry
        """,
        list(rows),
    )
    conn.commit()


def get_iv_history(conn: sqlite3.Connection, ticker: str, expiry: str, option_type: str, strike: float,
                    asof_date: str, lookback_days: int) -> list[float]:
    """Daily IV history for one contract, oldest to newest, within the lookback window (inclusive of asof_date)."""
    cur = conn.execute(
        """
        SELECT iv FROM chain_snapshots
        WHERE ticker = ? AND expiry = ? AND option_type = ? AND strike = ?
          AND asof_date <= ? AND asof_date >= date(?, ? || ' days')
          AND iv IS NOT NULL
        ORDER BY asof_date ASC
        """,
        (ticker, expiry, option_type, strike, asof_date, asof_date, f"-{lookback_days}"),
    )
    return [r[0] for r in cur.fetchall()]


def upsert_macro_gate(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO macro_gate (asof_date, run_timestamp, score, vix_level, vix_percentile, vix_score,
                                 vix3m_level, term_ratio, term_percentile, term_score, breadth_pct, breadth_score,
                                 credit_ratio, credit_percentile, credit_score,
                                 weight_vix, weight_term_structure, weight_breadth, weight_credit)
        VALUES (:asof_date, :run_timestamp, :score, :vix_level, :vix_percentile, :vix_score,
                :vix3m_level, :term_ratio, :term_percentile, :term_score, :breadth_pct, :breadth_score,
                :credit_ratio, :credit_percentile, :credit_score,
                :weight_vix, :weight_term_structure, :weight_breadth, :weight_credit)
        ON CONFLICT(asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, score=excluded.score, vix_level=excluded.vix_level,
            vix_percentile=excluded.vix_percentile, vix_score=excluded.vix_score, vix3m_level=excluded.vix3m_level,
            term_ratio=excluded.term_ratio, term_percentile=excluded.term_percentile, term_score=excluded.term_score,
            breadth_pct=excluded.breadth_pct, breadth_score=excluded.breadth_score, credit_ratio=excluded.credit_ratio,
            credit_percentile=excluded.credit_percentile, credit_score=excluded.credit_score,
            weight_vix=excluded.weight_vix, weight_term_structure=excluded.weight_term_structure,
            weight_breadth=excluded.weight_breadth, weight_credit=excluded.weight_credit
        """,
        row,
    )
    conn.commit()


def upsert_valuation(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO valuations (position_id, asof_date, run_timestamp, mark, current_value,
                                 pnl_dollar, pnl_pct, dte, progress_to_target_pct, progress_to_stop_pct,
                                 iv, delta, gamma, theta, vega)
        VALUES (:position_id, :asof_date, :run_timestamp, :mark, :current_value,
                :pnl_dollar, :pnl_pct, :dte, :progress_to_target_pct, :progress_to_stop_pct,
                :iv, :delta, :gamma, :theta, :vega)
        ON CONFLICT(position_id, asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, mark=excluded.mark, current_value=excluded.current_value,
            pnl_dollar=excluded.pnl_dollar, pnl_pct=excluded.pnl_pct, dte=excluded.dte,
            progress_to_target_pct=excluded.progress_to_target_pct,
            progress_to_stop_pct=excluded.progress_to_stop_pct,
            iv=excluded.iv, delta=excluded.delta, gamma=excluded.gamma, theta=excluded.theta, vega=excluded.vega
        """,
        row,
    )
    conn.commit()


def get_prior_valuation(conn: sqlite3.Connection, position_id: str, asof_date: str) -> Optional[dict]:
    """Most recent valuation for a position strictly before asof_date."""
    row = conn.execute(
        """
        SELECT mark, progress_to_target_pct, progress_to_stop_pct, iv, asof_date
        FROM valuations WHERE position_id = ? AND asof_date < ?
        ORDER BY asof_date DESC LIMIT 1
        """,
        (position_id, asof_date),
    ).fetchone()
    if row is None:
        return None
    return {"mark": row[0], "progress_to_target_pct": row[1], "progress_to_stop_pct": row[2],
            "iv": row[3], "asof_date": row[4]}


def upsert_chain_diff_rows(conn: sqlite3.Connection, asof_date: str, run_timestamp: str, entries: Iterable) -> None:
    # Rows have no natural unique key, so re-running the same asof_date replaces
    # rather than duplicates.
    conn.execute("DELETE FROM chain_diffs WHERE asof_date = ?", (asof_date,))
    values = [
        (asof_date, run_timestamp, e.ticker, e.kind, e.expiry, e.option_type, e.strike)
        for e in entries
    ]
    if values:
        conn.executemany(
            """
            INSERT INTO chain_diffs (asof_date, run_timestamp, ticker, kind, expiry, option_type, strike)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    conn.commit()


def get_chain_diffs(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    cur = conn.execute(
        "SELECT ticker, kind, expiry, option_type, strike FROM chain_diffs WHERE asof_date = ? "
        "ORDER BY ticker, kind, expiry, strike",
        (asof_date,),
    )
    return [{"ticker": r[0], "kind": r[1], "expiry": r[2], "option_type": r[3], "strike": r[4]}
            for r in cur.fetchall()]


def insert_alerts(conn: sqlite3.Connection, asof_date: str, rows: Iterable[dict]) -> None:
    # Same as chain_diffs: no natural unique key, so replace this date's alerts.
    conn.execute("DELETE FROM alerts WHERE asof_date = ?", (asof_date,))
    rows = list(rows)
    if rows:
        conn.executemany(
            """
            INSERT INTO alerts (asof_date, run_timestamp, alert_type, severity, ticker, position_id, message, detail)
            VALUES (:asof_date, :run_timestamp, :alert_type, :severity, :ticker, :position_id, :message, :detail)
            """,
            rows,
        )
    conn.commit()


def get_alerts(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    cur = conn.execute(
        "SELECT alert_type, severity, ticker, position_id, message, detail FROM alerts WHERE asof_date = ? "
        "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, ticker",
        (asof_date,),
    )
    return [{"alert_type": r[0], "severity": r[1], "ticker": r[2], "position_id": r[3],
              "message": r[4], "detail": r[5]} for r in cur.fetchall()]


# --- Dashboard read helpers ---------------------------------------------------

def get_latest_asof(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT MAX(asof_date) FROM valuations").fetchone()
    return row[0] if row and row[0] else None


def list_asof_dates(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT asof_date FROM valuations ORDER BY asof_date DESC")]


def get_positions(conn: sqlite3.Connection) -> list[dict]:
    cols = ["id", "asset_type", "ticker", "option_type", "strike", "expiry",
            "entry_price", "contracts", "entry_date", "target_price", "stop_price"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM positions")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_valuations(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    cols = ["position_id", "mark", "current_value", "pnl_dollar", "pnl_pct", "dte",
            "progress_to_target_pct", "progress_to_stop_pct", "iv", "delta", "gamma", "theta", "vega"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM valuations WHERE asof_date = ?", (asof_date,))
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_allocation(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    cols = ["group_type", "group_key", "value", "pct", "cap_pct", "flagged"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM allocation WHERE asof_date = ?", (asof_date,))
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_portfolio_greeks(conn: sqlite3.Connection, asof_date: str) -> Optional[dict]:
    cols = ["net_delta_shares", "daily_theta_dollars", "net_vega_dollars"]
    row = conn.execute(f"SELECT {', '.join(cols)} FROM portfolio_greeks WHERE asof_date = ?", (asof_date,)).fetchone()
    return dict(zip(cols, row)) if row else None


def get_macro_gate(conn: sqlite3.Connection, asof_date: str) -> Optional[dict]:
    cols = ["score", "vix_level", "vix_percentile", "vix_score", "vix3m_level", "term_ratio", "term_percentile",
            "term_score", "breadth_pct", "breadth_score", "credit_ratio", "credit_percentile", "credit_score"]
    row = conn.execute(f"SELECT {', '.join(cols)} FROM macro_gate WHERE asof_date = ?", (asof_date,)).fetchone()
    return dict(zip(cols, row)) if row else None


def get_news(conn: sqlite3.Connection, asof_date: str) -> list[dict]:
    cols = ["ticker", "window_days", "headline_count", "summary", "sentiment", "key_drivers",
            "position_flag", "model", "headlines_json"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM news_analysis WHERE asof_date = ? ORDER BY ticker", (asof_date,))
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_chain_rows(conn: sqlite3.Connection, ticker: str, expiry: str, option_type: str, asof_date: str) -> list[dict]:
    cols = ["strike", "bid", "ask", "last", "iv", "volume", "open_interest"]
    cur = conn.execute(
        f"SELECT {', '.join(cols)} FROM chain_snapshots "
        "WHERE ticker = ? AND expiry = ? AND option_type = ? AND asof_date = ? ORDER BY strike",
        (ticker, expiry, option_type, asof_date),
    )
    return [dict(zip(cols, row)) for row in cur.fetchall()]
