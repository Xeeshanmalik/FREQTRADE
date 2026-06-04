#!/bin/bash
# run_daily_cycle.sh — GapHunter morning cycle.
#
#   1. download fresh OHLCV data (5m/1h/4h/1d) into the mounted user_data
#   2. run the gap scanner INSIDE the container (needs pandas/talib)
#   3. (optional) Claude morning review -> claude_decision.json
#   4. update config.json whitelist + regime risk (host, stdlib only)
#   5. restart the bot to pick up today's watchlist
#
# Steps are defensive: a failure in the Claude step falls back to the scanner's
# own watchlist so the bot still trades a sane list.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # …/freqtrade/freqtrade
CONTAINER="freqtrade"
GAP_DIR="$REPO/user_data/gap_analysis"
SCORES="$GAP_DIR/daily_scores.json"
DECISION="$GAP_DIR/claude_decision.json"
PERF="$GAP_DIR/performance_7d.json"
CONFIG="$REPO/user_data/config.json"
DB="$REPO/user_data/tradesv3.sqlite"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] $*"; }

# ── Step 1: download fresh data ───────────────────────────────────────────────
log "Downloading market data (5m 1h 4h 1d, 30 days)…"
docker exec "$CONTAINER" freqtrade download-data \
  --timeframes 5m 1h 4h 1d \
  --days 30 \
  --config /freqtrade/user_data/config.json 2>&1 | tail -3

# ── Step 2: gap scanner (inside container) ────────────────────────────────────
log "Copying scanner scripts into container and running GapHunter…"
docker cp "$REPO/scripts/." "$CONTAINER:/freqtrade/scripts/" >/dev/null
# --min-score 40: the plan's 60 is unreachable in practice and the backtest-
# validated, profitable gate is 40 (see gap_score_threshold in the strategy).
docker exec "$CONTAINER" python3 /freqtrade/scripts/daily_gap_scan.py \
  --data-dir /freqtrade/user_data/data/binance \
  --output   /freqtrade/user_data/gap_analysis/daily_scores.json \
  --weights  /freqtrade/user_data/gap_analysis/score_weights.json \
  --min-score 40

# ── Step 3: refresh 7-day performance (host, stdlib) ──────────────────────────
if [[ -f "$DB" ]]; then
  log "Extracting 7-day performance…"
  python3 "$REPO/scripts/performance_extractor.py" --db "$DB" --days 7 --output "$PERF" || true
fi

# ── Step 4: Claude morning review (optional) ──────────────────────────────────
if command -v claude >/dev/null 2>&1; then
  log "Running Claude morning review…"
  TODAY=$(date -u +%Y-%m-%d)
  PROMPT="Today is $TODAY UTC.
## Gap Scanner Output
$(cat "$SCORES")
## Recent 7-Day Performance
$(cat "$PERF" 2>/dev/null || echo '{}')
$(cat "$REPO/scripts/prompts/morning_review.md")"
  if claude --print "$PROMPT" > "$DECISION" 2>/dev/null && [[ -s "$DECISION" ]]; then
    log "Claude decision written to $DECISION"
  else
    log "Claude review unavailable — falling back to scanner watchlist."
    python3 "$REPO/scripts/_scanner_to_decision.py" "$SCORES" "$DECISION"
  fi
else
  log "claude CLI not found — building decision from scanner watchlist."
  python3 "$REPO/scripts/_scanner_to_decision.py" "$SCORES" "$DECISION"
fi

# ── Step 5: update config + restart ───────────────────────────────────────────
log "Updating pair whitelist…"
python3 "$REPO/scripts/config_updater.py" --decisions "$DECISION" --config "$CONFIG"

log "Restarting freqtrade…"
docker restart "$CONTAINER" >/dev/null
log "Daily cycle complete. Bot trading today's watchlist."
