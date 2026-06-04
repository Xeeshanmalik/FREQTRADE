#!/usr/bin/env python3
"""
backtest_watchlists.py — generate historical, per-day GapHunter watchlists.

The live system runs the gap scanner once each morning and gates that day's
trading to the resulting watchlist. A normal freqtrade backtest cannot replay
that — it has no daily scan — so the coin-selection layer (the bulk of the
system's intended edge) is silently bypassed.

This script reconstructs it. For every day in a range it recomputes the
GapHunter watchlist using *only* OHLCV candles that closed strictly before that
day began (no lookahead), exactly as the 06:00 UTC scan would have seen them,
and writes a single JSON keyed by ISO date:

    {
      "min_score": 60.0,
      "max_pairs": 8,
      "scan_hour": 6,
      "generated_at": "2026-06-04T...",
      "days": {
        "2026-01-20": {"watchlist": ["ETH/USDT", ...],
                       "top": [{"pair": "ETH/USDT", "score": 41.2}, ...]},
        ...
      }
    }

UltraPrecisionStrategy.confirm_trade_entry consumes this in backtest/hyperopt
mode: a trade is allowed only if its pair is on that day's watchlist.

Usage:
    python3 backtest_watchlists.py \
        --data-dir user_data/data/binance \
        --output   user_data/gap_analysis/historical_watchlists.json \
        --start 2026-01-17 --end 2026-06-04
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coin_scorer import score_coin, select_daily_watchlist  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gaphunter-bt")

BTC_PAIR = "BTC/USDT"
# Scorer lookback needs: RS 168h (7d), volume profile 200h (~8d), 4h swing 50
# bars (~8d). 25 days is a comfortable margin and keeps the per-day slices small.
LOOKBACK_DAYS = 25


_OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def load_5m(data_dir: Path, pair: str) -> pd.DataFrame | None:
    f = data_dir / f"{pair.replace('/', '_')}-5m.feather"
    if not f.exists():
        return None
    df = pd.read_feather(f)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def resample_tf(df_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample 5m OHLCV up to a higher timeframe — the same view freqtrade builds
    from its @informative decorators, so generated scores match the backtest."""
    out = (
        df_5m.set_index("date").resample(rule, label="left", closed="left")
        .agg(_OHLCV_AGG).dropna(subset=["open"]).reset_index()
    )
    return out


def discover_pairs(data_dir: Path) -> list:
    return sorted(
        f.stem.split("-")[0].replace("_", "/") for f in data_dir.glob("*-5m.feather")
    )


def slice_before(df: pd.DataFrame, dates_ns: np.ndarray, cutoff: pd.Timestamp,
                 lookback_days: int) -> pd.DataFrame:
    """Rows with date in [cutoff - lookback_days, cutoff).

    Compares in integer nanoseconds since the epoch to avoid the tz-aware vs
    naive ``np.datetime64`` ambiguity that silently returns empty slices.
    """
    start_ns = (cutoff - pd.Timedelta(days=lookback_days)).value
    cutoff_ns = cutoff.value
    lo = int(np.searchsorted(dates_ns, start_ns))
    hi = int(np.searchsorted(dates_ns, cutoff_ns))
    return df.iloc[lo:hi]


def load_weights(weights_path: Path | None) -> dict:
    if weights_path and weights_path.exists():
        try:
            data = json.loads(weights_path.read_text())
            return data.get("weights", data)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate historical per-day GapHunter watchlists")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", default=None, help="First trading day (YYYY-MM-DD, UTC)")
    parser.add_argument("--end", default=None, help="Last trading day (YYYY-MM-DD, UTC)")
    parser.add_argument("--min-score", type=float, default=60.0)
    parser.add_argument("--max-pairs", type=int, default=8)
    parser.add_argument("--scan-hour", type=int, default=6, help="UTC hour the live scan runs")
    parser.add_argument("--weights", default=None)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    pairs = discover_pairs(data_dir)
    if not pairs:
        raise SystemExit(f"No pairs with 1h+4h data in {data_dir}")
    logger.info("Discovered %d pairs", len(pairs))

    logger.info("Loading 5m data and resampling to 1h/4h...")
    frames_1h, frames_4h = {}, {}
    for p in pairs:
        df5 = load_5m(data_dir, p)
        if df5 is None or df5.empty:
            continue
        frames_1h[p] = resample_tf(df5, "1h")
        frames_4h[p] = resample_tf(df5, "4h")
    pairs = sorted(frames_1h)
    btc_1h = frames_1h.get(BTC_PAIR)
    if btc_1h is None:
        raise SystemExit("BTC/USDT 5m data is required as the relative-strength anchor")

    # Precompute epoch-ns date arrays once for fast, tz-safe slicing.
    ns_1h = {p: df["date"].astype("int64").to_numpy() for p, df in frames_1h.items() if df is not None}
    ns_4h = {p: df["date"].astype("int64").to_numpy() for p, df in frames_4h.items() if df is not None}

    weights = load_weights(Path(args.weights) if args.weights else None)

    # Date span: bounded by data availability with a warmup margin.
    data_first = min(df["date"].iloc[0] for df in frames_1h.values() if df is not None and len(df))
    data_last = max(df["date"].iloc[-1] for df in frames_1h.values() if df is not None and len(df))
    first_day = pd.Timestamp(args.start, tz="UTC") if args.start else (
        data_first.normalize() + pd.Timedelta(days=LOOKBACK_DAYS)
    )
    last_day = pd.Timestamp(args.end, tz="UTC") if args.end else data_last.normalize()
    day_range = pd.date_range(first_day.normalize(), last_day.normalize(), freq="D", tz="UTC")
    logger.info("Generating watchlists for %d days: %s -> %s",
                len(day_range), day_range[0].date(), day_range[-1].date())

    days_out: dict = {}
    nonempty = 0
    max_scores: list = []
    for d in day_range:
        cutoff = d  # 00:00 UTC of trading day d
        btc_slice = slice_before(btc_1h, ns_1h[BTC_PAIR], cutoff, LOOKBACK_DAYS)
        if len(btc_slice) < 170:  # need >=7d of 1h for RS
            continue

        scores = []
        for pair in pairs:
            h = slice_before(frames_1h[pair], ns_1h[pair], cutoff, LOOKBACK_DAYS)
            f4 = slice_before(frames_4h[pair], ns_4h[pair], cutoff, LOOKBACK_DAYS)
            if h is None or len(h) < 50 or f4 is None or len(f4) < 12:
                continue
            try:
                res = score_coin(pair, h, h, f4, btc_slice,
                                 current_hour=args.scan_hour, weights=weights)
                scores.append(res)
            except Exception as exc:  # noqa: BLE001 — one bad pair must not kill the day
                logger.debug("[%s %s] scoring failed: %s", d.date(), pair, exc)

        scores.sort(key=lambda x: x["total_score"], reverse=True)
        watchlist = select_daily_watchlist(scores, min_score=args.min_score, max_pairs=args.max_pairs)
        days_out[d.strftime("%Y-%m-%d")] = {
            "watchlist": [c["pair"] for c in watchlist],
            "top": [{"pair": c["pair"], "score": c["total_score"]} for c in scores[:10]],
        }
        if watchlist:
            nonempty += 1
        if scores:
            max_scores.append(scores[0]["total_score"])

    payload = {
        "min_score": args.min_score,
        "max_pairs": args.max_pairs,
        "scan_hour": args.scan_hour,
        "weights_applied": weights or "default",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "days": days_out,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))

    # Diagnostics — is the threshold actually reachable historically?
    n = len(days_out)
    logger.info("Wrote %s — %d days, %d with a non-empty watchlist (%.0f%%)",
                out, n, nonempty, 100 * nonempty / n if n else 0)
    if max_scores:
        s = pd.Series(max_scores)
        logger.info("Daily best-score distribution: min=%.1f median=%.1f p90=%.1f max=%.1f",
                    s.min(), s.median(), s.quantile(0.9), s.max())
        for thr in (40, 50, 60, 70):
            days_hit = int((s >= thr).sum())
            logger.info("  threshold %2d: %d/%d days have >=1 eligible coin (%.0f%%)",
                        thr, days_hit, n, 100 * days_hit / n if n else 0)


if __name__ == "__main__":
    main()
