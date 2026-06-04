#!/usr/bin/env python3
"""
config_updater.py — apply Claude's daily decision to freqtrade's config.json.

Reads claude_decision.json (the morning review output) and rewrites config.json
so the bot trades exactly today's watchlist via a StaticPairList, with risk sizing
keyed to the declared market regime. Writes atomically and refuses to clobber the
config when the decision is empty or unparseable.
"""
import argparse
import json
import os
import tempfile
from pathlib import Path

# Risk posture per regime. BEAR flattens the book (0 open trades, 0 tradable ratio).
REGIME_SETTINGS = {
    "BULL": {"max_open_trades": 1, "tradable_balance_ratio": 0.95},
    "NEUTRAL": {"max_open_trades": 1, "tradable_balance_ratio": 0.80},
    "BEAR": {"max_open_trades": 0, "tradable_balance_ratio": 0.01},
}


def _normalise_watchlist(watchlist) -> list:
    """Accept either ['BTC/USDT', …] or [{'pair': 'BTC/USDT', …}, …]."""
    pairs = []
    for item in watchlist or []:
        if isinstance(item, str):
            pairs.append(item)
        elif isinstance(item, dict) and item.get("pair"):
            pairs.append(item["pair"])
    return pairs


def update_config(decisions_path: str, config_path: str) -> bool:
    decisions = json.loads(Path(decisions_path).read_text())
    config = json.loads(Path(config_path).read_text())

    watchlist = _normalise_watchlist(decisions.get("watchlist", []))
    regime = (decisions.get("market_regime") or "NEUTRAL").upper()

    if not watchlist:
        print("WARNING: empty watchlist from Claude — keeping existing config unchanged.")
        return False

    settings = REGIME_SETTINGS.get(regime, REGIME_SETTINGS["NEUTRAL"])
    config["max_open_trades"] = settings["max_open_trades"]
    config["tradable_balance_ratio"] = settings["tradable_balance_ratio"]

    config["exchange"]["pair_whitelist"] = watchlist
    # Static list for the curated watchlist + a spread guard so we never enter
    # into a blown-out book.
    config["pairlists"] = [
        {"method": "StaticPairList"},
        {"method": "SpreadFilter", "max_spread_ratio": 0.003},
    ]

    # Atomic write so a crashed run can't leave a half-written config.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config_path) or ".", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp, config_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    print(
        f"Config updated | regime={regime} "
        f"max_open_trades={settings['max_open_trades']} "
        f"ratio={settings['tradable_balance_ratio']} | watchlist={watchlist}"
    )
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply Claude decision to freqtrade config")
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    update_config(args.decisions, args.config)
