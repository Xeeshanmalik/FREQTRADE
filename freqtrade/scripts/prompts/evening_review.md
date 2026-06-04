You are a self-improving trading agent reviewing today's results.

## Today's Trades
{TRADES_JSON}

## Today's Watchlist Was
{WATCHLIST_JSON}

## Your Task
1. For each LOSING trade: identify which of the 6 gap signals was wrong or
   missing. Which gap type would have filtered this trade out?
2. For each WINNING trade: identify which gap signals were strongest.
3. Propose a score_weight_adjustment — a multiplier (0.8-1.2) for each of the 6
   gap dimensions based on today's evidence:
   fair_value_gap, volume_profile, relative_strength, fibonacci,
   liquidity_sweep, time_of_day.
4. Keep changes small (+/-20% max) and evidence-based. With few trades, prefer
   1.0 (no change) over guessing.

Output ONLY valid JSON in exactly this format (no prose, no code fences):

{
  "date": "YYYY-MM-DD",
  "win_rate_today": 0.0,
  "weight_adjustments": {
    "fair_value_gap": 1.0,
    "volume_profile": 1.0,
    "relative_strength": 1.0,
    "fibonacci": 1.0,
    "liquidity_sweep": 1.0,
    "time_of_day": 1.0
  },
  "key_learnings": ["...", "..."],
  "telegram_summary": "2-sentence summary for notification"
}
