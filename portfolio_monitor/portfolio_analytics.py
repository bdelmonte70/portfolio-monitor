"""Layer 2: allocation, aggregate greeks, and IV environment on top of layer 1's valuations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import db as db_mod
from .models import OptionPosition, Position
from .sectors import resolve_sector
from .valuation import Valuation


# --- Allocation ------------------------------------------------------------

@dataclass
class AllocationRow:
    group_type: str  # "ticker" | "sector"
    group_key: str
    value: float
    pct: float
    cap_pct: float
    flagged: bool


@dataclass
class AllocationResult:
    total_value: float
    by_ticker: list[AllocationRow]
    by_sector: list[AllocationRow]

    @property
    def flagged(self) -> list[AllocationRow]:
        return [r for r in self.by_ticker + self.by_sector if r.flagged]


def compute_allocation(positions: list[Position], valuations: list[Valuation], conn: sqlite3.Connection,
                        ticker_cap: float = 0.40, sector_cap: float = 0.60) -> AllocationResult:
    value_by_id = {v.position_id: v.current_value for v in valuations if v.current_value is not None}

    ticker_totals: dict[str, float] = {}
    for pos in positions:
        v = value_by_id.get(pos.id)
        if v is None:
            continue
        ticker_totals[pos.ticker] = ticker_totals.get(pos.ticker, 0.0) + v

    total_value = sum(ticker_totals.values())

    sector_of: dict[str, str] = {t: resolve_sector(t, conn) for t in ticker_totals}
    sector_totals: dict[str, float] = {}
    for ticker, v in ticker_totals.items():
        sector = sector_of[ticker]
        sector_totals[sector] = sector_totals.get(sector, 0.0) + v

    def build_rows(totals: dict[str, float], group_type: str, cap: float) -> list[AllocationRow]:
        rows = []
        for key, v in sorted(totals.items(), key=lambda kv: -kv[1]):
            pct = (v / total_value * 100.0) if total_value else 0.0
            rows.append(AllocationRow(group_type=group_type, group_key=key, value=v, pct=pct,
                                       cap_pct=cap * 100.0, flagged=pct > cap * 100.0))
        return rows

    return AllocationResult(
        total_value=total_value,
        by_ticker=build_rows(ticker_totals, "ticker", ticker_cap),
        by_sector=build_rows(sector_totals, "sector", sector_cap),
    )


# --- Aggregate greeks --------------------------------------------------------

@dataclass
class AggregateGreeks:
    net_delta_shares: float
    daily_theta_dollars: float
    net_vega_dollars: float


def compute_aggregate_greeks(positions: list[Position], valuations: list[Valuation]) -> AggregateGreeks:
    val_by_id = {v.position_id: v for v in valuations}

    net_delta = 0.0
    daily_theta = 0.0
    net_vega = 0.0

    for pos in positions:
        v = val_by_id.get(pos.id)
        if v is None:
            continue
        if isinstance(pos, OptionPosition):
            if v.delta is not None:
                net_delta += v.delta * pos.contracts * 100
            if v.theta is not None:
                daily_theta += v.theta * pos.contracts * 100
            if v.vega is not None:
                net_vega += v.vega * pos.contracts * 100
        else:
            net_delta += pos.contracts  # 1 share = 1 unit of underlying delta

    return AggregateGreeks(net_delta_shares=net_delta, daily_theta_dollars=daily_theta, net_vega_dollars=net_vega)


# --- IV environment ----------------------------------------------------------

@dataclass
class IVEnvironment:
    position_id: str
    current_iv: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    history_days: int
    status: str  # "building_history" | "ready"
    iv_flag: Optional[str]  # "rich" | "cheap" | "normal" | None
    dte: Optional[int]
    near_expiry: Optional[bool]


def _rank_and_percentile(current_iv: float, history: list[float]) -> tuple[float, float]:
    lo, hi = min(history), max(history)
    iv_rank = 50.0 if hi == lo else (current_iv - lo) / (hi - lo) * 100.0
    iv_percentile = sum(1 for h in history if h <= current_iv) / len(history) * 100.0
    return iv_rank, iv_percentile


def compute_iv_environment(positions: list[Position], valuations: list[Valuation], conn: sqlite3.Connection,
                            asof_date: date, lookback_days: int = 252, min_history_days: int = 20,
                            rich_threshold: float = 70.0, cheap_threshold: float = 30.0,
                            expiry_threshold_days: int = 45) -> list[IVEnvironment]:
    val_by_id = {v.position_id: v for v in valuations}
    asof_str = asof_date.isoformat()
    results = []

    for pos in positions:
        if not isinstance(pos, OptionPosition):
            continue
        v = val_by_id.get(pos.id)
        if v is None:
            continue

        history = db_mod.get_iv_history(conn, pos.ticker, pos.expiry, pos.option_type, pos.strike,
                                         asof_str, lookback_days)
        history_days = len(history)

        iv_rank = iv_percentile = None
        status = "building_history"
        iv_flag = None

        if history_days >= min_history_days and v.iv is not None:
            iv_rank, iv_percentile = _rank_and_percentile(v.iv, history)
            status = "ready"
            if iv_rank > rich_threshold:
                iv_flag = "rich"
            elif iv_rank < cheap_threshold:
                iv_flag = "cheap"
            else:
                iv_flag = "normal"

        near_expiry = (v.dte is not None and v.dte <= expiry_threshold_days)

        results.append(IVEnvironment(
            position_id=pos.id,
            current_iv=v.iv,
            iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            history_days=history_days,
            status=status,
            iv_flag=iv_flag,
            dte=v.dte,
            near_expiry=near_expiry,
        ))

    return results


# --- Persistence + summary ----------------------------------------------------

def store_analytics(conn: sqlite3.Connection, asof_str: str, run_timestamp: str,
                     allocation: AllocationResult, greeks: AggregateGreeks,
                     iv_env: list[IVEnvironment]) -> None:
    alloc_rows = [
        {"asof_date": asof_str, "run_timestamp": run_timestamp, "group_type": r.group_type,
         "group_key": r.group_key, "value": r.value, "pct": r.pct, "cap_pct": r.cap_pct,
         "flagged": int(r.flagged)}
        for r in allocation.by_ticker + allocation.by_sector
    ]
    db_mod.upsert_allocation_rows(conn, alloc_rows)

    db_mod.upsert_portfolio_greeks(conn, {
        "asof_date": asof_str, "run_timestamp": run_timestamp,
        "net_delta_shares": greeks.net_delta_shares,
        "daily_theta_dollars": greeks.daily_theta_dollars,
        "net_vega_dollars": greeks.net_vega_dollars,
    })

    iv_rows = [
        {"position_id": e.position_id, "asof_date": asof_str, "run_timestamp": run_timestamp,
         "current_iv": e.current_iv, "iv_rank": e.iv_rank, "iv_percentile": e.iv_percentile,
         "history_days": e.history_days, "status": e.status, "iv_flag": e.iv_flag,
         "dte": e.dte, "near_expiry": (int(e.near_expiry) if e.near_expiry is not None else None)}
        for e in iv_env
    ]
    db_mod.upsert_iv_environment_rows(conn, iv_rows)


def print_analytics_summary(asof_str: str, allocation: AllocationResult, greeks: AggregateGreeks,
                             iv_env: list[IVEnvironment], min_history_days: int) -> None:
    print(f"\n=== Portfolio Analytics as of {asof_str} ===")

    print(f"\nAllocation by Ticker (total value ${allocation.total_value:,.2f}):")
    for r in allocation.by_ticker:
        flag = f"  <- exceeds {r.cap_pct:.0f}% cap" if r.flagged else ""
        print(f"  {r.group_key:<8}{r.pct:>6.1f}%  (${r.value:,.2f}){flag}")

    print("\nAllocation by Sector:")
    for r in allocation.by_sector:
        flag = f"  <- exceeds {r.cap_pct:.0f}% cap" if r.flagged else ""
        print(f"  {r.group_key:<24}{r.pct:>6.1f}%{flag}")

    flagged = allocation.flagged
    print("\nConcentration Flags:")
    if flagged:
        for r in flagged:
            print(f"  - {r.group_key} ({r.group_type}) allocation {r.pct:.1f}% exceeds {r.cap_pct:.0f}% cap")
    else:
        print("  none")

    print("\nAggregate Greeks:")
    print(f"  Net delta (share-equivalent): {greeks.net_delta_shares:,.1f} shares")
    print(f"  Daily theta: ${greeks.daily_theta_dollars:,.2f}/day")
    print(f"  Net vega: ${greeks.net_vega_dollars:,.2f} per 1 IV point")

    print("\nIV Environment:")
    if not iv_env:
        print("  no option positions")
    for e in iv_env:
        iv_str = f"{e.current_iv * 100:.1f}%" if e.current_iv is not None else "n/a"
        if e.status == "building_history":
            status_str = f"building history ({e.history_days}/{min_history_days} days)"
        else:
            status_str = f"rank {e.iv_rank:.0f}  pctile {e.iv_percentile:.0f}"
            if e.iv_flag in ("rich", "cheap"):
                status_str += f"  <- {e.iv_flag}"
        near = "  <- near expiry" if e.near_expiry else ""
        dte_str = str(e.dte) if e.dte is not None else "-"
        print(f"  {e.position_id:<24} IV {iv_str:<8} {status_str:<32} DTE {dte_str}{near}")
