#!/usr/bin/env python3
"""
_scanner_to_decision.py — fallback decision builder (stdlib only).

When the Claude morning review is unavailable, convert the GapHunter scanner
output (daily_scores.json) directly into a claude_decision.json so the daily
cycle can still update the config. Regime defaults to NEUTRAL — the conservative
posture — since this path has no qualitative market read.

Usage: _scanner_to_decision.py <scores.json> <decision.json>
"""
import json
import sys
from datetime import datetime, timezone


def main(scores_path: str, decision_path: str) -> None:
    with open(scores_path) as f:
        scores = json.load(f)

    watchlist = [c["pair"] for c in scores.get("watchlist", []) if c.get("pair")]
    decision = {
        "date": scores.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        "market_regime": "NEUTRAL",
        "watchlist": watchlist,
        "excluded": [],
        "max_trades_today": 1,
        "confidence_note": "Auto-generated from scanner (Claude review unavailable).",
    }
    with open(decision_path, "w") as f:
        json.dump(decision, f, indent=2)
    print(f"Fallback decision written: {len(watchlist)} pairs -> {decision_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: _scanner_to_decision.py <scores.json> <decision.json>")
    main(sys.argv[1], sys.argv[2])
