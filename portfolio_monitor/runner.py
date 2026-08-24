"""Layer 4: the full pipeline entrypoint -- runs layers 1-3, diffs today's chain against
the prior run, raises condition alerts, stores everything, and (optionally) notifies.

This is what the cron job at the bottom of the module docstring calls once a day.
The system stays a monitor throughout: it reports conditions, tracks your own
targets, and Claude summarizes news. Nothing here recommends a trade or routes an order.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime

from . import alerts as alerts_mod
from . import cli as cli_mod
from . import db as db_mod
from . import diff as diff_mod
from . import news_analysis
from . import notifier
from . import portfolio_analytics as pa
from .config import Config
from .macro_gate import MacroGateWeights, compute_macro_gate, print_macro_gate_summary


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full portfolio monitor pipeline: layers 1-4")
    p.add_argument("--positions", default="positions.json")
    p.add_argument("--db", default="portfolio.db")
    p.add_argument("--snapshots-dir", default="snapshots")
    p.add_argument("--risk-free-rate", type=float, default=0.045)
    p.add_argument("--asof", default=None, help="YYYY-MM-DD run date (default: today)")

    p.add_argument("--ticker-cap", type=float, default=0.40)
    p.add_argument("--sector-cap", type=float, default=0.60)
    p.add_argument("--iv-lookback-days", type=int, default=252)
    p.add_argument("--iv-min-history-days", type=int, default=20)
    p.add_argument("--iv-rich-threshold", type=float, default=70.0)
    p.add_argument("--iv-cheap-threshold", type=float, default=30.0)
    p.add_argument("--expiry-threshold-days", type=int, default=45)

    p.add_argument("--weight-vix", type=float, default=0.25)
    p.add_argument("--weight-term-structure", type=float, default=0.25)
    p.add_argument("--weight-breadth", type=float, default=0.25)
    p.add_argument("--weight-credit", type=float, default=0.25)

    p.add_argument("--news-window-days", type=int, default=3)
    p.add_argument("--news-model", default=news_analysis.DEFAULT_MODEL)
    p.add_argument("--force-news-refresh", action="store_true")
    p.add_argument("--skip-news", action="store_true")

    p.add_argument("--diff-strike-band-pct", type=float, default=0.30,
                    help="new-strike diff band around held strikes, as a fraction (default 0.30 = +/-30%%)")
    p.add_argument("--target-near-pct", type=float, default=0.80,
                    help="fraction of the way to target that triggers a target_near alert (default 0.80)")
    p.add_argument("--iv-change-pct", type=float, default=0.20,
                    help="relative IV move vs prior snapshot that triggers an iv_change alert (default 0.20)")

    p.add_argument("--notify-telegram", action="store_true", help="push today's alerts to Telegram (off by default)")
    p.add_argument("--notify-imessage", action="store_true", help="push today's alerts via iMessage, macOS only (off by default)")
    p.add_argument("--imessage-recipient", default=None, help="iMessage recipient (phone/email) for --notify-imessage")
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

    # Layer 1
    result = cli_mod.execute_run(cfg, asof_date)
    cli_mod.print_valuation_summary(result.asof_str, result.valuations)

    # Layer 2
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

    # Layer 3
    weights = MacroGateWeights(vix=args.weight_vix, term_structure=args.weight_term_structure,
                                breadth=args.weight_breadth, credit=args.weight_credit)
    macro = compute_macro_gate(weights)
    macro_row = macro.to_dict()
    weights_dict = macro_row.pop("weights")
    macro_row.update(asof_date=result.asof_str, run_timestamp=result.run_timestamp,
                      weight_vix=weights_dict["vix"], weight_term_structure=weights_dict["term_structure"],
                      weight_breadth=weights_dict["breadth"], weight_credit=weights_dict["credit"])
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

    # Layer 4: diff + alerts + notifier
    tickers = sorted({p.ticker for p in result.positions})
    diff_entries = diff_mod.diff_all(result.conn, tickers, result.asof_str, result.positions,
                                      strike_band_pct=args.diff_strike_band_pct)
    db_mod.upsert_chain_diff_rows(result.conn, result.asof_str, result.run_timestamp, diff_entries)

    today_alerts = alerts_mod.collect_alerts(result.positions, result.valuations, result.conn, result.asof_str,
                                              diff_entries, news_results, target_near_pct=args.target_near_pct,
                                              iv_change_pct=args.iv_change_pct)
    alerts_mod.store_alerts(result.conn, result.asof_str, result.run_timestamp, today_alerts)
    alerts_mod.print_alerts(today_alerts)

    notifier_cfg = notifier.NotifierConfig(telegram=args.notify_telegram, imessage=args.notify_imessage,
                                            imessage_recipient=args.imessage_recipient)
    notifier.maybe_send(today_alerts, result.asof_str, notifier_cfg)

    result.conn.close()


if __name__ == "__main__":
    run()
