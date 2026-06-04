#!/bin/bash
# run_evening_cycle.sh — GapHunter evening learning cycle.
#
#   1. extract today's + 15-trade performance from the DB
#   2. (optional) Claude evening review -> weight_adjustments
#   3. fold the adjustments into score_weights.json (bounded 0.8-1.2, archived)
#   4. conditionally trigger hyperopt if the edge has decayed
#
# All steps are non-destructive to live trading; only the adaptive weight file
# and (optionally) hyperopt results change.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GAP_DIR="$REPO/user_data/gap_analysis"
DB="$REPO/user_data/tradesv3.sqlite"
PERF_TODAY="$GAP_DIR/performance_today.json"
PERF_15="$GAP_DIR/performance_15d.json"
WEIGHTS="$GAP_DIR/score_weights.json"
WEIGHT_HISTORY="$GAP_DIR/weight_history.json"
SCORES="$GAP_DIR/daily_scores.json"
EVAL="$GAP_DIR/evening_eval.json"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] $*"; }

# ── Step 1: performance snapshots ─────────────────────────────────────────────
log "Extracting performance snapshots…"
python3 "$REPO/scripts/performance_extractor.py" --db "$DB" --days 1  --output "$PERF_TODAY" || true
python3 "$REPO/scripts/performance_extractor.py" --db "$DB" --days 15 --output "$PERF_15"   || true

# ── Step 2: Claude evening review (optional) ──────────────────────────────────
if command -v claude >/dev/null 2>&1; then
  log "Running Claude evening review…"
  PROMPT="## Today's Trades
$(cat "$PERF_TODAY" 2>/dev/null || echo '{}')
## Today's Watchlist Was
$(cat "$SCORES" 2>/dev/null || echo '{}')
$(cat "$REPO/scripts/prompts/evening_review.md")"
  if claude --print "$PROMPT" > "$EVAL" 2>/dev/null && [[ -s "$EVAL" ]]; then
    log "Evening evaluation written to $EVAL"
    python3 "$REPO/scripts/apply_weights.py" \
      --eval "$EVAL" --weights "$WEIGHTS" --history "$WEIGHT_HISTORY" || true
  else
    log "Claude evening review unavailable — weights unchanged."
  fi
else
  log "claude CLI not found — skipping weight adaptation."
fi

# ── Step 3: conditional hyperopt ──────────────────────────────────────────────
log "Checking whether hyperopt is warranted…"
python3 "$REPO/scripts/hyperopt_trigger.py" --perf "$PERF_15" || true

log "Evening cycle complete."
