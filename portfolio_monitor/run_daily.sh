#!/bin/bash
# Runs the full pipeline (layers 1-4) once and logs to logs/runner_YYYY-MM-DD.log.
# Intended to be called from cron once each weekday after market close.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PARENT_DIR"  # `-m portfolio_monitor.runner` must run with the package's parent as cwd

mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/runner_$(date +%Y-%m-%d).log"

# ANTHROPIC_API_KEY (news analysis) and, if enabled, TELEGRAM_BOT_TOKEN /
# TELEGRAM_CHAT_ID (notifier) must already be set in the environment cron
# runs with -- cron does not inherit your interactive shell's env vars, so
# export them here or in a sourced file if they aren't set some other way.
# source "$SCRIPT_DIR/.env"

"$SCRIPT_DIR/.venv/bin/python" -m portfolio_monitor.runner \
    --positions "$SCRIPT_DIR/positions.json" \
    --db "$SCRIPT_DIR/portfolio.db" \
    --snapshots-dir "$SCRIPT_DIR/snapshots" \
    >> "$LOG_FILE" 2>&1
