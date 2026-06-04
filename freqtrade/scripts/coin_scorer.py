#!/usr/bin/env python3
"""
coin_scorer.py — The GapHunter master scoring engine.

Implements the 6 "gap" detectors described in IMPLEMENTATION_PLAN.md and a
master scorer that combines them into a single 0-100 conviction score per pair:

    Gap 1: Fair Value Gap (FVG) imbalance ............ 0-25
    Gap 2: Volume Profile void (LVN -> HVN) .......... 0-20
    Gap 3: Relative Strength vs BTC .................. 0-20
    Gap 4: Fibonacci retracement (golden pocket) ..... 0-15
    Gap 5: Liquidity sweep (stop hunt + reclaim) ..... 0-15
    Gap 6: Time-of-day volume window ................. 0-5
                                                       -----
                                              total    0-100

This module is import-safe (no side effects) so it can be reused by
daily_gap_scan.py and unit tests alike. Detectors are written to be defensive:
short or empty frames return a zero score rather than raising.
"""
from __future__ import annotations

import pandas as pd


# ---------------------------------------------------------------------------
# Gap Type 1: Fair Value Gap (FVG) — imbalance detection (0-25)
# ---------------------------------------------------------------------------
def detect_fair_value_gaps(df: pd.DataFrame, timeframe: str = "4h") -> list:
    """Scan for *unmitigated* bullish Fair Value Gaps.

    A bullish FVG is a 3-candle pattern where candle-1's high is below
    candle-3's low, leaving an untraded void. A gap is "mitigated" once a
    later candle trades back down into it. Returns gaps sorted by recency.
    """
    gaps: list = []
    if df is None or len(df) < 3:
        return gaps

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    dates = df["date"].to_numpy()
    n = len(df)

    for i in range(2, n):
        c1_high = highs[i - 2]
        c3_low = lows[i]

        # Bullish FVG: void between candle-1 high and candle-3 low
        if c1_high < c3_low:
            gap_bottom = c1_high
            gap_top = c3_low
            gap_size = (gap_top - gap_bottom) / gap_bottom if gap_bottom else 0.0

            # Mitigated if any *later* candle (after the 3-candle pattern)
            # traded back down into the gap.
            future_lows = lows[i + 1 :]
            is_mitigated = bool((future_lows <= gap_top).any()) if future_lows.size else False

            if not is_mitigated and gap_size >= 0.005:  # min 0.5% gap
                gaps.append(
                    {
                        "date": pd.Timestamp(dates[i - 1]).isoformat(),
                        "gap_bottom": float(gap_bottom),
                        "gap_top": float(gap_top),
                        "gap_size_pct": float(gap_size * 100),
                        "bars_ago": n - i,
                        "timeframe": timeframe,
                    }
                )

    return sorted(gaps, key=lambda g: g["bars_ago"])


def score_fvg(gaps: list, current_price: float) -> float:
    """Score 0-25 based on proximity to the nearest unmitigated bullish FVG."""
    if not gaps or not current_price:
        return 0.0

    nearest = min(gaps, key=lambda g: abs(current_price - g["gap_top"]))
    # Positive when price sits above the gap top (a magnet below it).
    distance_pct = (current_price - nearest["gap_top"]) / current_price

    score = 0.0
    if 0 <= distance_pct <= 0.05:  # price within 5% above the gap
        score += 15
        if distance_pct <= 0.02:  # approaching the gap top
            score += 5
        if nearest["gap_size_pct"] >= 1.0:  # large gap = stronger magnet
            score += 5

    return min(score, 25.0)


# ---------------------------------------------------------------------------
# Gap Type 2: Volume Profile void — resistance-free paths (0-20)
# ---------------------------------------------------------------------------
def build_volume_profile(df: pd.DataFrame, bins: int = 100) -> dict:
    """Build a price->volume histogram across the frame's price range."""
    if df is None or df.empty:
        return {"profile": {}, "bin_size": 0.0, "price_min": 0.0, "price_max": 0.0}

    price_min = float(df["low"].min())
    price_max = float(df["high"].max())
    span = price_max - price_min
    if span <= 0:
        return {"profile": {}, "bin_size": 0.0, "price_min": price_min, "price_max": price_max}

    bin_size = span / bins
    profile = {b: 0.0 for b in range(bins)}

    for low, high, vol in zip(df["low"].to_numpy(), df["high"].to_numpy(), df["volume"].to_numpy()):
        low_bin = int((low - price_min) / bin_size)
        high_bin = int((high - price_min) / bin_size)
        low_bin = max(0, min(low_bin, bins - 1))
        high_bin = max(0, min(high_bin, bins - 1))
        n_bins = max(high_bin - low_bin + 1, 1)
        vol_per_bin = vol / n_bins
        for b in range(low_bin, high_bin + 1):
            profile[b] += vol_per_bin

    return {"profile": profile, "bin_size": bin_size, "price_min": price_min, "price_max": price_max}


def score_volume_profile(vp: dict, current_price: float) -> float:
    """Score 0-20: is current price in a Low Volume Node with an HVN target above?"""
    profile = vp.get("profile", {})
    bin_size = vp.get("bin_size", 0.0)
    price_min = vp.get("price_min", 0.0)
    if not profile or bin_size <= 0 or not current_price:
        return 0.0

    avg_volume = sum(profile.values()) / len(profile)
    if avg_volume <= 0:
        return 0.0

    current_bin = int((current_price - price_min) / bin_size)
    if current_bin not in profile:
        return 0.0

    current_vol = profile[current_bin]
    next_hvn_bins = sorted(
        b for b, v in profile.items() if b > current_bin and v > avg_volume * 1.5
    )

    score = 0.0
    if current_vol < avg_volume * 0.4:  # in a Low Volume Node
        score += 12
        if next_hvn_bins:  # clear target overhead
            distance_to_hvn_pct = ((next_hvn_bins[0] - current_bin) * bin_size) / current_price
            if distance_to_hvn_pct >= 0.03:  # target at least 3% away
                score += 8

    return min(score, 20.0)


# ---------------------------------------------------------------------------
# Gap Type 3: Relative Strength vs BTC — sector rotation (0-20)
# ---------------------------------------------------------------------------
def calculate_relative_strength(coin_df: pd.DataFrame, btc_df: pd.DataFrame) -> dict:
    """Coin performance vs BTC over 1d/3d/7d. RS > 1.0 = outperforming BTC."""
    results: dict = {}
    if coin_df is None or btc_df is None or coin_df.empty or btc_df.empty:
        return results

    for period_hours in (24, 72, 168):
        if len(coin_df) <= period_hours or len(btc_df) <= period_hours:
            continue
        coin_chg = coin_df["close"].iloc[-1] / coin_df["close"].iloc[-period_hours] - 1
        btc_chg = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-period_hours] - 1
        denom = 1 + btc_chg
        if denom == 0:
            continue
        results[f"rs_{period_hours}h"] = (1 + coin_chg) / denom
    return results


def score_relative_strength(rs_data: dict) -> float:
    """Score 0-20 from 7d (accumulation) and 1d (momentum) relative strength."""
    score = 0.0

    rs_7d = rs_data.get("rs_168h", 1.0)
    if rs_7d >= 1.10:
        score += 10
    elif rs_7d >= 1.05:
        score += 7
    elif rs_7d >= 1.02:
        score += 4

    rs_1d = rs_data.get("rs_24h", 1.0)
    if rs_1d >= 1.03:
        score += 10
    elif rs_1d >= 1.01:
        score += 6
    elif rs_1d >= 1.00:
        score += 3

    return min(score, 20.0)


# ---------------------------------------------------------------------------
# Gap Type 4: Fibonacci retracement — institutional buy zones (0-15)
# ---------------------------------------------------------------------------
def find_major_swing(df: pd.DataFrame, lookback: int = 50) -> tuple:
    """Find the last major upleg (swing low -> swing high) in the window.

    We anchor on the lowest low, then take the highest high that occurs *after*
    it: that is the upleg whose retracement the Fibonacci levels describe. If the
    high never follows the low (a pure downtrend leg), we fall back to the global
    window high so callers still get a defined — if low-scoring — range rather
    than a degenerate one anchored on a tiny pre-high slice.
    """
    if df is None or df.empty:
        return 0.0, 0.0

    window = df.tail(lookback).reset_index(drop=True)
    low_pos = int(window["low"].idxmin())
    swing_low = float(window["low"].iloc[low_pos])

    post_low = window.iloc[low_pos:]
    swing_high = float(post_low["high"].max())
    if swing_high <= swing_low:  # no upleg after the low — degenerate
        swing_high = float(window["high"].max())

    return swing_high, swing_low


def calculate_fib_levels(swing_high: float, swing_low: float) -> dict:
    diff = swing_high - swing_low
    return {
        "0.236": swing_high - 0.236 * diff,
        "0.382": swing_high - 0.382 * diff,
        "0.500": swing_high - 0.500 * diff,
        "0.618": swing_high - 0.618 * diff,
        "0.650": swing_high - 0.650 * diff,  # golden pocket edge
        "0.786": swing_high - 0.786 * diff,
    }


def score_fibonacci(fib_levels: dict, current_price: float) -> float:
    """Score 0-15 based on proximity to key Fibonacci levels (golden pocket = best)."""
    if not current_price:
        return 0.0

    tolerance = 0.015  # within 1.5% of a level
    fib_scores = {"0.618": 10, "0.650": 10, "0.500": 7, "0.382": 5, "0.786": 4, "0.236": 2}

    best = 0.0
    for level, pts in fib_scores.items():
        fib_price = fib_levels.get(level, 0)
        if fib_price > 0:
            distance = abs(current_price - fib_price) / fib_price
            if distance <= tolerance:
                best = max(best, pts)

    return min(best, 15.0)


# ---------------------------------------------------------------------------
# Gap Type 5: Liquidity sweep — stop hunt + reclaim (0-15)
# ---------------------------------------------------------------------------
def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> dict:
    """Detect a recent sweep: price wicks below a stop cluster, then closes back above."""
    result = {"detected": False, "strength": 0.0, "bars_ago": None}
    if df is None or len(df) <= lookback:
        return result

    lows = df["low"]
    n = len(df)
    for i in range(lookback, n):
        window_lows = lows.iloc[i - lookback : i]
        support_level = window_lows.quantile(0.15)  # approximate stop cluster

        candle = df.iloc[i]
        if candle["low"] < support_level and candle["close"] > support_level:
            sweep_depth = (support_level - candle["low"]) / support_level if support_level else 0.0
            bars_ago = n - 1 - i
            if bars_ago <= 5:  # only recent sweeps matter
                result = {
                    "detected": True,
                    "strength": float(min(sweep_depth * 10, 1.0)),
                    "bars_ago": int(bars_ago),
                    "swept_level": float(support_level),
                }

    return result


def score_liquidity_sweep(sweep: dict) -> float:
    """Score 0-15. Recency decays fast — a fresh sweep is far more potent."""
    if not sweep.get("detected"):
        return 0.0

    recency_multiplier = {0: 1.0, 1: 0.9, 2: 0.75, 3: 0.6, 4: 0.45, 5: 0.3}
    mult = recency_multiplier.get(sweep.get("bars_ago", 5), 0.3)
    return min(15 * sweep["strength"] * mult, 15.0)


# ---------------------------------------------------------------------------
# Gap Type 6: Time-of-day volume window (0-5)
# ---------------------------------------------------------------------------
def analyze_time_of_day(df: pd.DataFrame) -> dict:
    """Find the UTC hours in which this coin trades the most volume."""
    if df is None or df.empty:
        return {"peak_hours": [], "hourly_profile": {}}

    df = df.copy()
    df["hour"] = pd.to_datetime(df["date"], utc=True).dt.hour
    hourly_vol = df.groupby("hour")["volume"].mean()
    peak_hours = hourly_vol.nlargest(8).index.tolist()
    return {"peak_hours": sorted(int(h) for h in peak_hours), "hourly_profile": hourly_vol.to_dict()}


def score_time_of_day(time_analysis: dict, current_utc_hour: int) -> float:
    """Score 0-5: are we inside the coin's peak-volume window right now?"""
    return 5.0 if current_utc_hour in time_analysis.get("peak_hours", []) else 0.0


# ---------------------------------------------------------------------------
# Master scorer + watchlist selection
# ---------------------------------------------------------------------------
# Sector map drives diversification in select_daily_watchlist(). Extend freely;
# unknown pairs fall back to the "other" bucket.
SECTOR_MAP = {
    "BTC/USDT": "store-of-value",
    "ETH/USDT": "smart-contract",
    "SOL/USDT": "smart-contract",
    "AVAX/USDT": "smart-contract",
    "BNB/USDT": "exchange",
    "MATIC/USDT": "layer2",
    "POL/USDT": "layer2",
    "ARB/USDT": "layer2",
    "OP/USDT": "layer2",
    "UNI/USDT": "defi",
    "AAVE/USDT": "defi",
    "CRV/USDT": "defi",
    "LINK/USDT": "oracle",
    "XRP/USDT": "payments",
    "ADA/USDT": "smart-contract",
    "DOGE/USDT": "meme",
    "SHIB/USDT": "meme",
    "DOT/USDT": "interoperability",
    "ATOM/USDT": "interoperability",
    "NEAR/USDT": "smart-contract",
    "APT/USDT": "smart-contract",
    "SUI/USDT": "smart-contract",
    "INJ/USDT": "defi",
    "RNDR/USDT": "ai",
    "FET/USDT": "ai",
    "TAO/USDT": "ai",
    "LTC/USDT": "payments",
}


def score_coin(
    pair: str,
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    btc_df: pd.DataFrame,
    current_hour: int | None = None,
    weights: dict | None = None,
) -> dict:
    """Run all 6 gap detectors for one pair and return a scored result dict.

    ``weights`` is an optional per-dimension multiplier map (the adaptive weights
    written by the evening review). Each raw sub-score is multiplied by its weight
    then clamped back to its dimension cap so the total stays within 0-100.
    """
    current_price = float(df_1h["close"].iloc[-1])
    if current_hour is None:
        current_hour = pd.Timestamp.utcnow().hour

    weights = weights or {}

    # Run detectors
    fvg_gaps = detect_fair_value_gaps(df_4h, timeframe="4h")
    vp = build_volume_profile(df_1h.tail(200), bins=100)
    rs_data = calculate_relative_strength(df_1h, btc_df)
    sh, sl = find_major_swing(df_4h, lookback=50)
    fib_levels = calculate_fib_levels(sh, sl)
    sweep = detect_liquidity_sweep(df_1h, lookback=20)
    time_analysis = analyze_time_of_day(df_1h)

    # Raw sub-scores with their dimension caps
    raw = {
        "fair_value_gap": (score_fvg(fvg_gaps, current_price), 25.0),
        "volume_profile": (score_volume_profile(vp, current_price), 20.0),
        "relative_strength": (score_relative_strength(rs_data), 20.0),
        "fibonacci": (score_fibonacci(fib_levels, current_price), 15.0),
        "liquidity_sweep": (score_liquidity_sweep(sweep), 15.0),
        "time_of_day": (score_time_of_day(time_analysis, current_hour), 5.0),
    }

    breakdown = {}
    for dim, (value, cap) in raw.items():
        adjusted = min(value * weights.get(dim, 1.0), cap)
        breakdown[dim] = round(adjusted, 2)

    total_score = round(sum(breakdown.values()), 2)

    return {
        "pair": pair,
        "total_score": total_score,
        "breakdown": breakdown,
        "current_price": current_price,
        "sector": SECTOR_MAP.get(pair, "other"),
        "rs": {k: round(v, 4) for k, v in rs_data.items()},
        "fvg_count": len(fvg_gaps),
        "liquidity_sweep": sweep,
        "timestamp": pd.Timestamp.utcnow().isoformat(),
    }


def select_daily_watchlist(
    all_scores: list, min_score: float = 60.0, max_pairs: int = 8, max_per_sector: int = 2
) -> list:
    """Select today's watchlist from scored coins.

    Rules:
      - must score >= ``min_score`` (default 60/100)
      - no more than ``max_per_sector`` coins from one sector (diversification)
      - cap total at ``max_pairs``
    """
    eligible = [c for c in all_scores if c["total_score"] >= min_score]
    eligible.sort(key=lambda x: x["total_score"], reverse=True)

    selected: list = []
    sector_count: dict = {}
    for coin in eligible:
        if len(selected) >= max_pairs:
            break
        sector = coin["sector"]
        if sector_count.get(sector, 0) >= max_per_sector:
            continue
        selected.append(coin)
        sector_count[sector] = sector_count.get(sector, 0) + 1

    return selected
