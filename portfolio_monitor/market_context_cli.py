"""CLI entrypoint for layer 3: runs layers 1+2, then the macro gate and daily Claude news analysis."""

from __future__ import annotations

import argparse
from datetime import date, datetime

from . import cli as cli_mod
from . import db as db_mod
from . import news_analysis
from . import portfolio_analytics as pa
from .config import Config
from .macro_gate import MacroGateWeights, compute_macro_gate, print_macro_gate_summary


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Market context layer (macro gate + daily news analysis)")
    p.add_argument("--positions", default="positions.json", help="path to positions.json")
    p.add_argument("--db", default="portfolio.db", help="path to SQLite database")
    p.add_argument("--snapshots-dir", default="snapshots", help="directory for dated chain snapshots")
    p.add_argument("--risk-free-rate", type=float, default=0.045, help="annual risk-free rate, e.g. 0.045")
    p.add_argument("--asof", default=None, help="YYYY-MM-DD run date (default: today)")

    p.add_argument("--ticker-cap", type=float, default=0.40)
    p.add_argument("--sector-cap", type=float, default=0.60)
    p.add_argument("--iv-lookback-days", type=int, default=252)
    p.add_argument("--iv-min-history-days", type=int, default=20)
    p.add_argument("--iv-rich-threshold", type=float, default=70.0)
    p.add_argument("--iv-cheap-threshold", type=float, default=30.0)
    p.add_argument("--expiry-threshold-days", type=int, default=45)

    p.add_argument("--weight-vix", type=float, default=0.25, help="macro gate weight for VIX level/percentile")
    p.add_argument("--weight-term-structure", type=float, default=0.25, help="macro gate weight for VIX term structure")
    p.add_argument("--weight-breadth", type=float, default=0.25, help="macro gate weight for market breadth")
    p.add_argument("--weight-credit", type=float, default=0.25, help="macro gate weight for credit spread (HYG/TLT)")

    p.add_argument("--news-window-days", type=int, default=3, help="lookback window for headlines (default 3)")
    p.add_argument("--news-model", default=news_analysis.DEFAULT_MODEL, help="Claude model for news analysis")
    p.add_argument("--force-news-refresh", action="store_true",
                    help="re-call Claude even if today's analysis is already cached")
    p.add_argument("--skip-news", action="store_true", help="skip the Claude news analysis step entirely")
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

    weights = MacroGateWeights(vix=args.weight_vix, term_structure=args.weight_term_structure,
                                breadth=args.weight_breadth, credit=args.weight_credit)
    macro = compute_macro_gate(weights)
    macro_row = macro.to_dict()
    weights_dict = macro_row.pop("weights")
    macro_row["asof_date"] = result.asof_str
    macro_row["run_timestamp"] = result.run_timestamp
    macro_row["weight_vix"] = weights_dict["vix"]
    macro_row["weight_term_structure"] = weights_dict["term_structure"]
    macro_row["weight_breadth"] = weights_dict["breadth"]
    macro_row["weight_credit"] = weights_dict["credit"]
    db_mod.upsert_macro_gate(result.conn, macro_row)
    print_macro_gate_summary(result.asof_str, macro)

    if args.skip_news:
        news_results = []
    else:
        news_results = news_analysis.run_news_analysis(
            result.positions, result.conn, result.asof_date, result.run_timestamp,
            model=args.news_model, window_days=args.news_window_days, force_refresh=args.force_news_refresh,
        )
    news_analysis.print_news_summary(news_results)

    result.conn.close()


if __name__ == "__main__":
    run()
