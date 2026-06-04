#!/usr/bin/env python3
"""
daily_gap_scan.py — GapHunter daily orchestrator.

Loads OHLCV feather data (as produced by ``freqtrade download-data``) for every
available pair, runs the 6-gap scoring engine in coin_scorer.py, applies any
adaptive scoring weights, selects the daily watchlist, and writes the result to
a JSON file consumed by the Claude morning review and the UltraPrecision strategy.

Usage:
    python3 daily_gap_scan.py \
        --data-dir user_data/data/binance \
        --output   user_data/gap_analysis/daily_scores.json

Data file naming follows freqtrade's convention, e.g. ``BTC_USDT-1h.feather``.
The scanner needs 5m, 1h and 4h frames per pair plus BTC as the RS anchor.
Pairs missing required timeframes are skipped with a warning rather than failing
the whole run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

# Allow running both as a module and as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coin_scorer import score_coin, select_daily_watchlist  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gaphunter")

REQUIRED_TIMEFRAMES = ("5m", "1h", "4h")
BTC_PAIR = "BTC/USDT"


def _file_pair(stem: str) -> str:
    """Turn a feather stem like ``BTC_USDT-1h`` into a pair ``BTC/USDT``."""
    name = stem.split("-")[0]
    return name.replace("_", "/")


def discover_pairs(data_dir: Path) -> list:
    """Return pairs that have *all* required timeframe feathers present."""
    by_pair: dict = {}
    for f in data_dir.glob("*.feather"):
        stem = f.stem  # e.g. BTC_USDT-1h
        if "-" not in stem:
            continue
        pair = _file_pair(stem)
        tf = stem.split("-")[-1]
        by_pair.setdefault(pair, set()).add(tf)

    complete = [p for p, tfs in by_pair.items() if all(t in tfs for t in REQUIRED_TIMEFRAMES)]
    incomplete = sorted(set(by_pair) - set(complete))
    if incomplete:
        logger.warning("Skipping %d pair(s) missing timeframes: %s", len(incomplete), incomplete)
    return sorted(complete)


def load_frame(data_dir: Path, pair: str, timeframe: str) -> pd.DataFrame | None:
    fname = data_dir / f"{pair.replace('/', '_')}-{timeframe}.feather"
    if not fname.exists():
        return None
    df = pd.read_feather(fname)
    # freqtrade feathers use a 'date' column already; normalise to UTC datetime.
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], utc=True)
    return df


def load_weights(weights_path: Path) -> dict:
    """Load adaptive per-dimension weights written by the evening review."""
    if weights_path and weights_path.exists():
        try:
            data = json.loads(weights_path.read_text())
            weights = data.get("weights", data)
            logger.info("Loaded adaptive weights: %s", weights)
            return weights
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read weights %s: %s — using defaults", weights_path, exc)
    return {}


def run_scan(
    data_dir: Path,
    output: Path,
    min_score: float,
    max_pairs: int,
    weights_path: Path | None,
    current_hour: int | None,
) -> dict:
    pairs = discover_pairs(data_dir)
    if not pairs:
        raise SystemExit(f"No pairs with complete timeframes found in {data_dir}")

    btc_1h = load_frame(data_dir, BTC_PAIR, "1h")
    if btc_1h is None:
        logger.warning("BTC/USDT 1h data missing — relative-strength scores will be 0.")

    weights = load_weights(weights_path) if weights_path else {}

    all_scores: list = []
    for pair in pairs:
        try:
            df_5m = load_frame(data_dir, pair, "5m")
            df_1h = load_frame(data_dir, pair, "1h")
            df_4h = load_frame(data_dir, pair, "4h")
            if df_1h is None or df_1h.empty or len(df_1h) < 50:
                logger.warning("[%s] insufficient 1h data — skipping", pair)
                continue
            result = score_coin(
                pair,
                df_5m,
                df_1h,
                df_4h,
                btc_1h if btc_1h is not None else df_1h,
                current_hour=current_hour,
                weights=weights,
            )
            all_scores.append(result)
            logger.info("[%s] score=%5.1f  %s", pair, result["total_score"], result["breakdown"])
        except Exception as exc:  # noqa: BLE001 — one bad pair must not kill the scan
            logger.error("[%s] scoring failed: %s", pair, exc)

    all_scores.sort(key=lambda x: x["total_score"], reverse=True)
    watchlist = select_daily_watchlist(all_scores, min_score=min_score, max_pairs=max_pairs)

    payload = {
        "date": pd.Timestamp.utcnow().strftime("%Y-%m-%d"),
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "min_score": min_score,
        "pairs_scanned": len(all_scores),
        "weights_applied": weights or "default",
        "watchlist": watchlist,
        "all_scores": all_scores,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2))
    logger.info(
        "Wrote %s — %d scored, %d on watchlist (min %.0f)",
        output,
        len(all_scores),
        len(watchlist),
        min_score,
    )
    if watchlist:
        logger.info("Watchlist: %s", [f"{c['pair']}={c['total_score']}" for c in watchlist])
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="GapHunter daily gap scanner")
    parser.add_argument("--data-dir", required=True, help="Directory of *.feather OHLCV files")
    parser.add_argument("--output", required=True, help="Path to write daily_scores.json")
    parser.add_argument("--min-score", type=float, default=60.0, help="Watchlist threshold")
    parser.add_argument("--max-pairs", type=int, default=8, help="Max coins on the watchlist")
    parser.add_argument("--weights", default=None, help="Optional score_weights.json path")
    parser.add_argument(
        "--current-hour",
        type=int,
        default=None,
        help="Override UTC hour for time-of-day scoring (mainly for tests)",
    )
    args = parser.parse_args()

    run_scan(
        data_dir=Path(args.data_dir),
        output=Path(args.output),
        min_score=args.min_score,
        max_pairs=args.max_pairs,
        weights_path=Path(args.weights) if args.weights else None,
        current_hour=args.current_hour,
    )


if __name__ == "__main__":
    main()
