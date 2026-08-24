"""Per-position valuation: mark, current value, unrealized P&L, DTE, progress to target/stop."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

from . import market_data
from .greeks import compute_greeks
from .models import OptionPosition, SharePosition


@dataclass
class Valuation:
    position_id: str
    ticker: str
    asset_type: str
    mark: Optional[float]
    current_value: Optional[float]
    pnl_dollar: Optional[float]
    pnl_pct: Optional[float]
    dte: Optional[int]
    progress_to_target_pct: Optional[float]
    progress_to_stop_pct: Optional[float]
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _mid_or_last(bid, ask, last) -> Optional[float]:
    # Treat non-positive bid/ask as "no quote" (common for illiquid/stale contracts)
    # rather than a real 0.0 price, so we fall back to last instead of marking at 0.
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last


def _progress_pct(mark: Optional[float], entry: float, target: Optional[float]) -> Optional[float]:
    """% of the way from entry toward target (>100% means mark is past target).

    Assumes a long position: favorable movement is (mark - entry) in the
    direction of (target - entry). Same convention is used for stop, where
    the favorable direction is reversed (entry -> stop is the adverse move).
    """
    if mark is None or target is None:
        return None
    denom = target - entry
    if denom == 0:
        return None
    return (mark - entry) / denom * 100.0


def value_option(position: OptionPosition, chain: dict, spot: Optional[float],
                  asof: date, risk_free_rate: float) -> Valuation:
    row = market_data.find_contract_row(chain, position.expiry, position.strike, position.option_type)

    bid = row.get("bid") if row else None
    ask = row.get("ask") if row else None
    last = row.get("last") if row else None
    iv = row.get("iv") if row else None

    mark = _mid_or_last(bid, ask, last)

    dte = (position.expiry_date - asof).days

    current_value = mark * 100 * position.contracts if mark is not None else None
    entry_value = position.entry_price * 100 * position.contracts
    pnl_dollar = (current_value - entry_value) if current_value is not None else None
    pnl_pct = ((mark - position.entry_price) / position.entry_price * 100.0
               if mark is not None and position.entry_price else None)

    g = compute_greeks(
        spot=spot,
        strike=position.strike,
        days_to_expiry=dte,
        risk_free_rate=risk_free_rate,
        iv=iv,
        option_type=position.option_type,
    ) if spot is not None and iv is not None else None

    return Valuation(
        position_id=position.id,
        ticker=position.ticker,
        asset_type="option",
        mark=mark,
        current_value=current_value,
        pnl_dollar=pnl_dollar,
        pnl_pct=pnl_pct,
        dte=dte,
        progress_to_target_pct=_progress_pct(mark, position.entry_price, position.target_price),
        progress_to_stop_pct=_progress_pct(mark, position.entry_price, position.stop_price),
        iv=iv,
        delta=g.delta if g else None,
        gamma=g.gamma if g else None,
        theta=g.theta if g else None,
        vega=g.vega if g else None,
    )


def value_shares(position: SharePosition, spot: Optional[float]) -> Valuation:
    mark = spot
    current_value = mark * position.contracts if mark is not None else None
    entry_value = position.entry_price * position.contracts
    pnl_dollar = (current_value - entry_value) if current_value is not None else None
    pnl_pct = ((mark - position.entry_price) / position.entry_price * 100.0
               if mark is not None and position.entry_price else None)

    return Valuation(
        position_id=position.id,
        ticker=position.ticker,
        asset_type="shares",
        mark=mark,
        current_value=current_value,
        pnl_dollar=pnl_dollar,
        pnl_pct=pnl_pct,
        dte=None,
        progress_to_target_pct=_progress_pct(mark, position.entry_price, position.target_price),
        progress_to_stop_pct=_progress_pct(mark, position.entry_price, position.stop_price),
    )
