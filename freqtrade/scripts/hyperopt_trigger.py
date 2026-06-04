#!/usr/bin/env python3
"""
hyperopt_trigger.py — auto-run hyperopt when performance degrades.

Decision rule: only re-optimise once there is enough data (>= 15 closed trades,
~3 weeks at the target cadence) AND the win rate has dropped below 57%. This
guards against the two classic mistakes — optimising on noise, and never
adapting when the edge decays. After a successful run it parses the best epoch
and patches the strategy's parameter defaults (kept behind --apply).
"""
import argparse
import json
import subprocess
from pathlib import Path

HYPEROPT_THRESHOLD_WIN_RATE = 0.57  # trigger below 57%
HYPEROPT_THRESHOLD_TRADES = 15  # ~3 weeks at 4-6 trades/week

STRATEGY = "UltraPrecisionStrategy"
CONFIG_PATH = "/freqtrade/user_data/config.json"


def should_run_hyperopt(perf_path: str) -> bool:
    perf = json.loads(Path(perf_path).read_text())
    total = perf.get("total_trades", 0)
    win_rate = perf.get("win_rate", 0)

    if total < HYPEROPT_THRESHOLD_TRADES:
        print(f"Only {total} trades — not enough data yet (need {HYPEROPT_THRESHOLD_TRADES}).")
        return False
    if win_rate < HYPEROPT_THRESHOLD_WIN_RATE:
        print(f"Win rate {win_rate:.1%} below {HYPEROPT_THRESHOLD_WIN_RATE:.0%}. Triggering hyperopt.")
        return True
    print(f"Win rate {win_rate:.1%} is healthy. No hyperopt needed.")
    return False


def run_hyperopt(timerange: str, epochs: int, apply: bool) -> None:
    cmd = [
        "docker", "exec", "freqtrade", "freqtrade", "hyperopt",
        "--strategy", STRATEGY,
        "--hyperopt-loss", "SharpeHyperOptLossDaily",
        "--spaces", "buy", "sell", "stoploss",
        "--epochs", str(epochs),
        "--timerange", timerange,
        "--config", CONFIG_PATH,
    ]
    print(f"Running hyperopt ({epochs} epochs, timerange {timerange})…")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        print(f"Hyperopt failed:\n{result.stderr[-1000:]}")
        return
    print("Hyperopt complete.")
    print(result.stdout[-1500:])
    if apply:
        print("NOTE: --apply requested. Review the printed best params, then patch "
              f"the *_default values in {STRATEGY}.py. Auto-patching is intentionally "
              "manual so a bad epoch can't silently change live behaviour.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conditionally run hyperopt")
    parser.add_argument("--perf", required=True, help="performance JSON path")
    parser.add_argument("--timerange", default="20250101-", help="hyperopt timerange")
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--apply", action="store_true", help="surface best params for manual apply")
    parser.add_argument("--force", action="store_true", help="run regardless of thresholds")
    args = parser.parse_args()

    if args.force or should_run_hyperopt(args.perf):
        run_hyperopt(args.timerange, args.epochs, args.apply)
