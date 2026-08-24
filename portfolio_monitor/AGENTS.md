# portfolio_monitor — agent brief

Read this before touching the project. It's a Python portfolio **monitor**
for a mixed options + equity book, built in four layers, all on top of one
SQLite database. It is explicitly not a trading system.

## Hard boundary — read this first

This tool reports conditions, tracks the user's own targets/stops, and has
Claude summarize news. **Nothing in this codebase recommends a trade,
sizes a position, or routes an order, and it must stay that way.** If asked
to extend this project, do not add trade signals, buy/sell recommendations,
or order execution — flag it back to the user instead. `alerts.py` and
`news_analysis.py` both encode this constraint explicitly (see their
docstrings); preserve it in anything you add.

## Layers

1. **Data + valuation** (`models.py`, `market_data.py`, `greeks.py`,
   `snapshots.py`, `valuation.py`, `cli.py`) — loads `positions.json`, pulls
   yfinance quotes/chains, computes Black-Scholes greeks locally (no
   external lib), values every position, snapshots the full chain per
   underlying to `snapshots/TICKER_YYYY-MM-DD.json` + SQLite.
2. **Portfolio analytics** (`sectors.py`, `portfolio_analytics.py`,
   `analytics_cli.py`) — allocation by ticker/sector with concentration
   flags, aggregate portfolio greeks, IV rank/percentile per option.
3. **Market context** (`macro_gate.py`, `news_analysis.py`,
   `market_context_cli.py`) — a deterministic 0-100 macro score (VIX,
   term structure, breadth, credit spread) and a daily Claude-generated
   news read per held ticker, cached per ticker/day so reruns don't re-bill.
4. **Diff, alerts, dashboard, notifier** (`diff.py`, `alerts.py`,
   `notifier.py`, `runner.py`, `dashboard.py`) — diffs today's chain vs.
   the prior snapshot, raises condition alerts, ties everything together,
   and (opt-in) pushes alerts to Telegram/iMessage.

Each layer's CLI (`cli.py`, `analytics_cli.py`, `market_context_cli.py`)
still runs standalone if you only need that layer's output. `runner.py` is
the superset — it runs all four and is what production usage should call.

## Entrypoints

Always activate the venv first and run from the **parent** of
`portfolio_monitor/` (package-relative imports require it):

```bash
cd "/Users/brenden/Claude Code/Trading System"
source portfolio_monitor/.venv/bin/activate
python -m portfolio_monitor.runner \
    --positions portfolio_monitor/positions.json \
    --db portfolio_monitor/portfolio.db \
    --snapshots-dir portfolio_monitor/snapshots
```

Or just run `portfolio_monitor/run_daily.sh` — it resolves paths correctly
on its own and logs to `portfolio_monitor/logs/`. That script is the one
to put on a schedule; see the scheduling conflict note below.

Dashboard (reads SQLite only — no yfinance/Claude calls, safe to open
anytime, run `runner.py` first to populate data):

```bash
cd "/Users/brenden/Claude Code/Trading System/portfolio_monitor"
PORTFOLIO_DB_PATH=portfolio.db .venv/bin/streamlit run dashboard.py
```

## Data model

Single SQLite file (`portfolio.db` by default). Key tables:

- `positions` — parsed `positions.json`, upserted each run
- `chain_snapshots` — full option chain per underlying per `asof_date`
  (this is what `diff.py` and IV-rank compute off of)
- `valuations` — mark/P&L/greeks per position per `asof_date`
- `allocation`, `portfolio_greeks`, `iv_environment` — layer 2 output
- `macro_gate`, `news_analysis` — layer 3 output (`news_analysis` also
  stores the raw headlines used, as `headlines_json`, for the dashboard's
  clickable links)
- `chain_diffs`, `alerts` — layer 4 output. Both are cleared and
  re-inserted per `asof_date` on every run (no natural unique key), so
  querying `WHERE asof_date = ?` always gets that day's full picture.

Full schema is in `db.py`; use its query helpers (`get_positions`,
`get_valuations`, `get_alerts`, etc.) rather than writing raw SQL against
these tables where one already exists.

## Editing positions.json

Plain JSON, safe to hand-edit. Two row shapes (`asset_type: "option"` or
`"shares"`) — see `models.py` for the exact fields. For options,
`entry_price`/`target_price`/`stop_price` are **per-share premium**, not
per-contract (each contract = 100 shares). After any edit, validate before
trusting it downstream:

```bash
python -c "from portfolio_monitor.models import load_positions; load_positions('positions.json')"
```

This raises a clear `PositionError` if something's malformed, rather than
failing confusingly mid-pipeline.

## Environment variables required

- `ANTHROPIC_API_KEY` — layer 3 news analysis. Without it, `runner.py`
  prints a warning and skips that step; everything else still runs.
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — only if `notifier.py`'s
  Telegram path is enabled (`--notify-telegram`). Off by default.

None of these are stored in the repo. `run_daily.sh` has a commented
`source .env` line as a place to wire them in for cron, since cron doesn't
inherit an interactive shell's environment.

## Known limitations (by design, not oversights)

- **Same-day reruns re-fire `target_hit`/`stop_hit` alerts.** Crossing
  detection compares against the most recent strictly-earlier `asof_date`,
  so running the pipeline twice in one day re-sends high-severity alerts
  rather than suppressing the repeat. Fine for the intended "once daily
  after close" cadence; **do not run the pipeline a second time on the
  same day if a notifier is enabled**, or you'll double-notify.
- **Don't schedule the pipeline from two places at once.** If Hermes is
  going to run `run_daily.sh` on its own schedule, remove the cron entry
  (or vice versa) — same reason as above.
- Market breadth uses the 11 SPDR sector ETFs (% above their own 200dma)
  as a fast proxy, not all ~500 S&P constituents.
- VIX term structure falls back to a fixed contango/backwardation band
  when there's insufficient `^VIX3M` history (yfinance/Yahoo carries very
  little of it) — see `macro_gate.py` for the exact fallback.
- `target_price`/`stop_price` are treated as premium-space levels for
  options (same units as `entry_price`/mark), not underlying-price levels.

## Adding to this project

- Keep new tables/queries in `db.py`, following the existing
  upsert-by-primary-key pattern (or clear-and-reinsert for tables with no
  natural key, like `alerts`/`chain_diffs`).
- Keep the "monitor, not trader" boundary from the top of this doc.
- If you touch `requirements.txt`, reinstall
  (`pip install -r requirements.txt`) so `.venv` doesn't drift between
  whichever agent/session touches it next.
