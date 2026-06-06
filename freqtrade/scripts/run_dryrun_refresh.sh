#!/bin/bash
# run_dryrun_refresh.sh — minimal daily refresh for the UltraPrecision forward
# dry-run. Unlike run_daily_cycle.sh (the full agentic loop with Claude review,
# regime risk-sizing and a bot restart), this only does what the validated
# Design-B forward test needs:
#
#   1. download fresh OHLCV so the scanner sees current candles
#   2. re-run the GapHunter scan at the chosen gate (min-score 40, for more activity)
#
# It does NOT restart the bot or rewrite the pairlist: the pair_whitelist is a
# fixed 25-pair universe and UltraPrecisionStrategy.confirm_trade_entry reloads
# daily_scores.json automatically whenever its mtime changes. Fewer moving parts
# = a cleaner forward read on the system we actually backtested.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # …/freqtrade/freqtrade
CONTAINER="freqtrade"
GAP_DIR="$REPO/user_data/gap_analysis"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] $*"; }

log "Downloading market data (5m 1h 4h 1d, 30 days)…"
docker exec "$CONTAINER" freqtrade download-data \
  --timeframes 5m 1h 4h 1d \
  --days 30 \
  --config /freqtrade/user_data/config.json 2>&1 | tail -2

log "Refreshing GapHunter scan (min-score 40)…"
docker cp "$REPO/scripts/." "$CONTAINER:/tmp/gh_scripts/" >/dev/null
# Paths below are CONTAINER paths (user_data is mounted at /freqtrade/user_data).
docker exec "$CONTAINER" python3 /tmp/gh_scripts/daily_gap_scan.py \
  --data-dir /freqtrade/user_data/data/binance \
  --output   /freqtrade/user_data/gap_analysis/daily_scores.json \
  --weights  /freqtrade/user_data/gap_analysis/score_weights.json \
  --min-score 40 2>&1 | tail -1

WL=$(python3 -c "import json;print([w['pair'] for w in json.load(open('$GAP_DIR/daily_scores.json'))['watchlist']])")
log "Watchlist refreshed: $WL (strategy reloads it on next candle)"
