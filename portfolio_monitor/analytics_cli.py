"""CLI entrypoint for layer 2: runs layer 1, then computes + stores + prints portfolio analytics."""

from __future__ import annotations

import argparse
from datetime import date, datetime

from . import cli as cli_mod
from . import portfolio_analytics as pa
from .config import Config


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Portfolio analytics layer (allocation, greeks, IV environment)")
    p.add_argument("--positions", default="positions.json", help="path to positions.json")
    p.add_argument("--db", default="portfolio.db", help="path to SQLite database")
    p.add_argument("--snapshots-dir", default="snapshots", help="directory for dated chain snapshots")
    p.add_argument("--risk-free-rate", type=float, default=0.045, help="annual risk-free rate, e.g. 0.045")
    p.add_argument("--asof", default=None, help="YYYY-MM-DD run date (default: today)")

    p.add_argument("--ticker-cap", type=float, default=0.40, help="concentration warning cap per ticker, as a fraction (default 0.40)")
    p.add_argument("--sector-cap", type=float, default=0.60, help="concentration warning cap per sector, as a fraction (default 0.60)")

    p.add_argument("--iv-lookback-days", type=int, default=252, help="lookback window for IV rank/percentile (default 252)")
    p.add_argument("--iv-min-history-days", type=int, default=20, help="minimum snapshot days before IV rank is computed (default 20)")
    p.add_argument("--iv-rich-threshold", type=float, default=70.0, help="IV rank above which a contract is flagged rich (default 70)")
    p.add_argument("--iv-cheap-threshold", type=float, default=30.0, help="IV rank below which a contract is flagged cheap (default 30)")

    p.add_argument("--expiry-threshold-days", type=int, default=45, help="DTE at/below which a position is flagged near-expiry (default 45)")
    return p.parse_args(argv)


def run(argv=None) -> None:
    args = parse_args(argv)
    cfg = Config(
        risk_free_rate=args.risk_free_rate,
        positions_path=args.positions,
        db_path=args.db,
        snapshots_dir=args.snapshots_dir,
    )
    asof_date = (datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today())

    result = cli_mod.execute_run(cfg, asof_date)
    cli_mod.print_valuation_summary(result.asof_str, result.valuations)

    allocation = pa.compute_allocation(result.positions, result.valuations, result.conn,
                                        ticker_cap=args.ticker_cap, sector_cap=args.sector_cap)
    greeks = pa.compute_aggregate_greeks(result.positions, result.valuations)
    iv_env = pa.compute_iv_environment(result.positions, result.valuations, result.conn, result.asof_date,
                                        lookback_days=args.iv_lookback_days,
                                        min_history_days=args.iv_min_history_days,
                                        rich_threshold=args.iv_rich_threshold,
                                        cheap_threshold=args.iv_cheap_threshold,
                                        expiry_threshold_days=args.expiry_threshold_days)

    pa.store_analytics(result.conn, result.asof_str, result.run_timestamp, allocation, greeks, iv_env)
    pa.print_analytics_summary(result.asof_str, allocation, greeks, iv_env, args.iv_min_history_days)

    result.conn.close()


if __name__ == "__main__":
    run()
