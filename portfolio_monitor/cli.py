"""CLI entrypoint: pull data, snapshot chains, compute greeks + valuation, store to SQLite."""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from . import db as db_mod
from . import market_data
from . import snapshots
from .config import Config
from .models import OptionPosition, SharePosition, Position, load_positions
from .valuation import Valuation, value_option, value_shares


@dataclass
class RunResult:
    conn: sqlite3.Connection
    positions: list[Position]
    valuations: list[Valuation]
    spots: dict[str, float | None]
    chains: dict[str, dict]
    asof_date: date
    asof_str: str
    run_timestamp: str


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Options + equity portfolio data/valuation layer")
    p.add_argument("--positions", default="positions.json", help="path to positions.json")
    p.add_argument("--db", default="portfolio.db", help="path to SQLite database")
    p.add_argument("--snapshots-dir", default="snapshots", help="directory for dated chain snapshots")
    p.add_argument("--risk-free-rate", type=float, default=0.045, help="annual risk-free rate, e.g. 0.045")
    p.add_argument("--asof", default=None,
                    help="YYYY-MM-DD run date to stamp for storage/diffing (default: today). "
                         "Marks still come from the live feed; this does not reconstruct historical chains.")
    return p.parse_args(argv)


def execute_run(cfg: Config, asof_date: date) -> RunResult:
    """Pull data, snapshot chains, value every position, and persist it all to SQLite.

    Leaves the connection open so callers (e.g. the analytics layer) can run
    further queries/writes against the same run before closing it.
    """
    asof_str = asof_date.isoformat()
    run_timestamp = datetime.now().isoformat(timespec="seconds")

    positions = load_positions(cfg.positions_path)
    conn = db_mod.connect(cfg.db_path)
    db_mod.upsert_positions(conn, positions)

    tickers = sorted({p.ticker for p in positions})
    option_tickers = sorted({p.ticker for p in positions if isinstance(p, OptionPosition)})

    spots: dict[str, float | None] = {}
    chains: dict[str, dict] = {}

    for ticker in tickers:
        quote = market_data.get_spot_quote(ticker)
        spots[ticker] = quote.last
        db_mod.upsert_underlying_quote(conn, ticker, asof_str, run_timestamp, quote.last)

    for ticker in option_tickers:
        chain = market_data.get_full_chain(ticker)
        chains[ticker] = chain
        path = snapshots.save_chain_snapshot(chain, ticker, asof_str, cfg.snapshots_dir, conn)
        print(f"snapshot saved: {path}")

    results = []
    for pos in positions:
        if isinstance(pos, OptionPosition):
            val = value_option(pos, chains.get(pos.ticker, {"expiries": {}}), spots.get(pos.ticker),
                                asof_date, cfg.risk_free_rate)
        else:
            val = value_shares(pos, spots.get(pos.ticker))

        row = val.to_dict()
        row["asof_date"] = asof_str
        row["run_timestamp"] = run_timestamp
        db_mod.upsert_valuation(conn, row)
        results.append(val)

    return RunResult(
        conn=conn,
        positions=positions,
        valuations=results,
        spots=spots,
        chains=chains,
        asof_date=asof_date,
        asof_str=asof_str,
        run_timestamp=run_timestamp,
    )


def run(argv=None) -> None:
    args = parse_args(argv)
    cfg = Config(
        risk_free_rate=args.risk_free_rate,
        positions_path=args.positions,
        db_path=args.db,
        snapshots_dir=args.snapshots_dir,
    )
    asof_date = (datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today())

    result = execute_run(cfg, asof_date)
    print_valuation_summary(result.asof_str, result.valuations)
    result.conn.close()


def print_valuation_summary(asof_str: str, results) -> None:
    print(f"\nValuation as of {asof_str}")
    header = f"{'ID':<24}{'Ticker':<8}{'Mark':>10}{'Value':>12}{'P&L $':>12}{'P&L %':>9}{'DTE':>6}{'->Tgt%':>9}{'->Stop%':>9}"
    print(header)
    print("-" * len(header))
    for v in results:
        mark = f"{v.mark:.2f}" if v.mark is not None else "n/a"
        value = f"{v.current_value:,.2f}" if v.current_value is not None else "n/a"
        pnl_d = f"{v.pnl_dollar:,.2f}" if v.pnl_dollar is not None else "n/a"
        pnl_p = f"{v.pnl_pct:.1f}" if v.pnl_pct is not None else "n/a"
        dte = str(v.dte) if v.dte is not None else "-"
        tgt = f"{v.progress_to_target_pct:.0f}" if v.progress_to_target_pct is not None else "-"
        stop = f"{v.progress_to_stop_pct:.0f}" if v.progress_to_stop_pct is not None else "-"
        print(f"{v.position_id:<24}{v.ticker:<8}{mark:>10}{value:>12}{pnl_d:>12}{pnl_p:>9}{dte:>6}{tgt:>9}{stop:>9}")


if __name__ == "__main__":
    run()
