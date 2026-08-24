"""Diff today's chain snapshot against the prior run per underlying.

Surfaces new expiries and new strikes near what's actually held, without
scanning the whole chain by hand.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from .models import OptionPosition, Position


@dataclass
class ChainDiffEntry:
    ticker: str
    kind: str  # "new_expiry" | "new_strike"
    expiry: str
    option_type: Optional[str]  # None for new_expiry (applies to both sides)
    strike: Optional[float]  # None for new_expiry


def _prior_asof(conn: sqlite3.Connection, ticker: str, asof_str: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(asof_date) FROM chain_snapshots WHERE ticker = ? AND asof_date < ?",
        (ticker, asof_str),
    ).fetchone()
    return row[0] if row and row[0] else None


def _held_strikes(positions: list[Position], ticker: str) -> list[float]:
    return [p.strike for p in positions if isinstance(p, OptionPosition) and p.ticker == ticker]


def diff_chain(conn: sqlite3.Connection, ticker: str, asof_str: str, positions: list[Position],
                strike_band_pct: float = 0.30) -> list[ChainDiffEntry]:
    """New expiries/strikes for one underlying vs its most recent prior snapshot.

    Returns nothing the first time a ticker is ever snapshotted (no prior run
    to diff against). New strikes are limited to a band around currently
    held strikes for that ticker (default +/-30%); strikes belonging to a
    brand-new expiry are reported once, under new_expiry, not duplicated.
    """
    prior_asof = _prior_asof(conn, ticker, asof_str)
    if prior_asof is None:
        return []

    today_rows = conn.execute(
        "SELECT DISTINCT expiry, option_type, strike FROM chain_snapshots WHERE ticker = ? AND asof_date = ?",
        (ticker, asof_str),
    ).fetchall()
    prior_rows = conn.execute(
        "SELECT DISTINCT expiry, option_type, strike FROM chain_snapshots WHERE ticker = ? AND asof_date = ?",
        (ticker, prior_asof),
    ).fetchall()

    today_expiries = {r[0] for r in today_rows}
    prior_expiries = {r[0] for r in prior_rows}
    new_expiries = today_expiries - prior_expiries

    today_contracts = {(r[0], r[1], r[2]) for r in today_rows}
    prior_contracts = {(r[0], r[1], r[2]) for r in prior_rows}

    entries: list[ChainDiffEntry] = []
    for expiry in sorted(new_expiries):
        entries.append(ChainDiffEntry(ticker=ticker, kind="new_expiry", expiry=expiry, option_type=None, strike=None))

    held_strikes = _held_strikes(positions, ticker)
    lo = min(held_strikes) * (1 - strike_band_pct) if held_strikes else None
    hi = max(held_strikes) * (1 + strike_band_pct) if held_strikes else None

    for expiry, option_type, strike in sorted(today_contracts - prior_contracts):
        if expiry in new_expiries:
            continue  # already reported as part of a brand-new expiry
        if lo is not None and not (lo <= strike <= hi):
            continue
        entries.append(ChainDiffEntry(ticker=ticker, kind="new_strike", expiry=expiry,
                                       option_type=option_type, strike=strike))

    return entries


def diff_all(conn: sqlite3.Connection, tickers: list[str], asof_str: str, positions: list[Position],
              strike_band_pct: float = 0.30) -> list[ChainDiffEntry]:
    entries: list[ChainDiffEntry] = []
    for ticker in tickers:
        entries.extend(diff_chain(conn, ticker, asof_str, positions, strike_band_pct))
    return entries
