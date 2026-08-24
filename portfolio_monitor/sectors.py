"""Ticker -> sector resolution: config lookup first, yfinance .info as fallback, cached in SQLite."""

from __future__ import annotations

import sqlite3
import sys
from typing import Optional

import yfinance as yf

# Config lookup: fast, offline, no rate limits. Extend as your book grows.
# ETFs/indices are bucketed as "ETF" rather than a GICS sector since they
# don't have one.
DEFAULT_SECTOR_MAP: dict[str, str] = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "GOOGL": "Technology",
    "GOOG": "Technology",
    "AMZN": "Consumer Discretionary",
    "META": "Technology",
    "NVDA": "Technology",
    "TSLA": "Consumer Discretionary",
    "JPM": "Financials",
    "BAC": "Financials",
    "XOM": "Energy",
    "CVX": "Energy",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "SPY": "ETF",
    "QQQ": "ETF",
    "IWM": "ETF",
    "DIA": "ETF",
    "VOO": "ETF",
}


def _lookup_yfinance(ticker: str) -> Optional[str]:
    try:
        t = yf.Ticker(ticker)
        info = t.get_info()
    except Exception as exc:
        print(f"warning: could not fetch sector info for {ticker}: {exc}", file=sys.stderr)
        return None

    sector = info.get("sector")
    if sector:
        return sector
    if info.get("quoteType") == "ETF":
        return "ETF"
    return None


def resolve_sector(ticker: str, conn: sqlite3.Connection) -> str:
    """Resolve a ticker's sector: config map -> cached DB lookup -> live yfinance -> "Unknown".

    Any newly-resolved (non-config) sector is cached in the sector_map table
    so repeat runs don't re-hit yfinance for the same ticker.
    """
    if ticker in DEFAULT_SECTOR_MAP:
        return DEFAULT_SECTOR_MAP[ticker]

    row = conn.execute("SELECT sector FROM sector_map WHERE ticker = ?", (ticker,)).fetchone()
    if row is not None:
        return row[0]

    sector = _lookup_yfinance(ticker) or "Unknown"
    conn.execute(
        "INSERT INTO sector_map (ticker, sector, source) VALUES (?, ?, 'yfinance') "
        "ON CONFLICT(ticker) DO UPDATE SET sector=excluded.sector, source=excluded.source",
        (ticker, sector),
    )
    conn.commit()
    return sector
