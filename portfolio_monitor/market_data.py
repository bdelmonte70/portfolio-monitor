"""yfinance data pulls: underlying quotes and full option chains."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Optional

import yfinance as yf


@dataclass
class Quote:
    ticker: str
    last: Optional[float]
    bid: Optional[float] = None
    ask: Optional[float] = None


def get_spot_quote(ticker: str) -> Quote:
    """Current spot price for an underlying (used as mark for shares and as S for greeks)."""
    t = yf.Ticker(ticker)
    last = None
    try:
        fi = t.fast_info
        last = fi.get("last_price") if isinstance(fi, dict) else getattr(fi, "last_price", None)
    except Exception:
        last = None

    if last is None or (isinstance(last, float) and math.isnan(last)):
        try:
            hist = t.history(period="1d")
            if not hist.empty:
                last = float(hist["Close"].iloc[-1])
        except Exception as exc:
            print(f"warning: could not fetch spot price for {ticker}: {exc}", file=sys.stderr)
            last = None

    return Quote(ticker=ticker, last=last)


def _row_to_dict(row) -> dict:
    def clean(v):
        if v is None:
            return None
        try:
            if isinstance(v, float) and math.isnan(v):
                return None
        except TypeError:
            pass
        return v

    return {
        "contract_symbol": clean(row.get("contractSymbol")),
        "strike": clean(row.get("strike")),
        "bid": clean(row.get("bid")),
        "ask": clean(row.get("ask")),
        "last": clean(row.get("lastPrice")),
        "iv": clean(row.get("impliedVolatility")),
        "volume": clean(row.get("volume")),
        "open_interest": clean(row.get("openInterest")),
        "in_the_money": clean(row.get("inTheMoney")),
    }


def get_full_chain(ticker: str) -> dict:
    """Pull the full option chain (every listed expiry) for a ticker.

    Returns: {"ticker": ..., "expiries": {expiry_str: {"calls": [...], "puts": [...]}}}
    """
    t = yf.Ticker(ticker)
    try:
        expiries = list(t.options)
    except Exception as exc:
        print(f"warning: could not list expiries for {ticker}: {exc}", file=sys.stderr)
        expiries = []

    chain: dict = {"ticker": ticker, "expiries": {}}
    for expiry in expiries:
        try:
            oc = t.option_chain(expiry)
        except Exception as exc:
            print(f"warning: could not fetch chain for {ticker} {expiry}: {exc}", file=sys.stderr)
            continue
        calls = [_row_to_dict(r) for r in oc.calls.to_dict("records")]
        puts = [_row_to_dict(r) for r in oc.puts.to_dict("records")]
        chain["expiries"][expiry] = {"calls": calls, "puts": puts}

    return chain


def find_contract_row(chain: dict, expiry: str, strike: float, option_type: str) -> Optional[dict]:
    """Find the matching row in a full chain pulled via get_full_chain()."""
    expiry_data = chain.get("expiries", {}).get(expiry)
    if not expiry_data:
        return None
    rows = expiry_data["calls"] if option_type == "call" else expiry_data["puts"]
    for row in rows:
        if row.get("strike") is not None and math.isclose(row["strike"], strike, rel_tol=1e-9, abs_tol=1e-6):
            return row
    return None
