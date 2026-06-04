#!/usr/bin/env python3
"""
apply_weights.py — fold Claude's evening weight adjustments into score_weights.json.

The evening review proposes a per-dimension multiplier (0.8-1.2). We multiply the
current persisted weight by that suggestion, clamp the running weight to a sane
band, archive every change to weight_history.json, and write the new weights for
the next morning's scan to pick up. Stdlib only.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

DIMENSIONS = [
    "fair_value_gap",
    "volume_profile",
    "relative_strength",
    "fibonacci",
    "liquidity_sweep",
    "time_of_day",
]

# Hard band on the *cumulative* weight so drift can't run away over many days.
WEIGHT_MIN, WEIGHT_MAX = 0.5, 1.5
# Per-day suggestion is itself clamped so one noisy day can't swing things hard.
STEP_MIN, STEP_MAX = 0.8, 1.2


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def load_weights(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("weights", data)
        except (json.JSONDecodeError, OSError):
            pass
    return {d: 1.0 for d in DIMENSIONS}


def main(eval_path: str, weights_path: str, history_path: str) -> None:
    evaluation = json.loads(Path(eval_path).read_text())
    adjustments = evaluation.get("weight_adjustments", {})
    if not adjustments:
        print("No weight_adjustments in evaluation — nothing to do.")
        return

    weights = load_weights(Path(weights_path))
    updated = {}
    for dim in DIMENSIONS:
        current = float(weights.get(dim, 1.0))
        step = _clamp(float(adjustments.get(dim, 1.0)), STEP_MIN, STEP_MAX)
        updated[dim] = round(_clamp(current * step, WEIGHT_MIN, WEIGHT_MAX), 4)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "weights": updated,
        "source_date": evaluation.get("date"),
    }
    Path(weights_path).write_text(json.dumps(payload, indent=2))

    # Append-only audit trail.
    history = []
    hp = Path(history_path)
    if hp.exists():
        try:
            history = json.loads(hp.read_text())
        except (json.JSONDecodeError, OSError):
            history = []
    history.append({
        "date": evaluation.get("date"),
        "applied_at": payload["updated_at"],
        "adjustments": adjustments,
        "resulting_weights": updated,
        "win_rate_today": evaluation.get("win_rate_today"),
    })
    hp.write_text(json.dumps(history, indent=2))

    print(f"Weights updated: {updated}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply evening weight adjustments")
    parser.add_argument("--eval", required=True, help="evening_eval.json")
    parser.add_argument("--weights", required=True, help="score_weights.json")
    parser.add_argument("--history", required=True, help="weight_history.json")
    args = parser.parse_args()
    main(args.eval, args.weights, args.history)
