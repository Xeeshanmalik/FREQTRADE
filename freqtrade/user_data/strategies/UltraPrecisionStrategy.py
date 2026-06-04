# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
UltraPrecisionStrategy — the execution engine of the GapHunter system.

It assumes the GapHunter scanner (scripts/daily_gap_scan.py) has already written
today's high-conviction watchlist to user_data/gap_analysis/daily_scores.json.
This strategy then applies 7 layers of confirmation before any entry:

    Layer 1: Market Regime Gate   — no longs while BTC < EMA200(1d)
    Layer 2: 4h Trend Confirmation — EMA stack + ADX + RSI ceiling
    Layer 3: 1h Trend Confirmation — EMA stack + RSI band + MACD
    Layer 4: 5m Entry Timing       — StochRSI cross + volume + BB floor
    Layer 5: AI Pre-Entry Veto     — confirm_trade_entry() checks the watchlist
    Layer 6: ATR Dynamic Exit      — custom_stoploss() trails on volatility
    Layer 7: Exit Signal           — momentum exhaustion / trend break / volume dry-up

Target profile: 4-6 trades/week, 62-70% win rate, R/R >= 2.0. See
IMPLEMENTATION_PLAN.md for the full rationale behind every threshold.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
from freqtrade.strategy import (
    IStrategy,
    IntParameter,
    DecimalParameter,
    informative,
    merge_informative_pair,
)

logger = logging.getLogger(__name__)

# Watchlist written by the GapHunter scanner each morning. Resolved relative to
# this file so it works identically on host and inside the freqtrade container
# (…/user_data/strategies/ -> …/user_data/gap_analysis/).
WATCHLIST_PATH = Path(__file__).resolve().parents[1] / "gap_analysis" / "daily_scores.json"
REGIME_ANCHOR = "BTC/USDT"


class UltraPrecisionStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"

    # Only act on closed candles; entries are deliberate, not tick-chasing.
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    can_short = False

    # EMA200 on the 4h informative is the heaviest lookback. Freqtrade extends
    # the startup window for informative pairs automatically; 200 base candles
    # keeps the 5m indicators warm.
    startup_candle_count = 200

    # ── Hard stop floor (custom_stoploss tightens above this) ──────────────────
    stoploss = -0.08

    # ── ROI ladder — the 6h/12h rungs force capital turnover so the single
    #    trade slot is never locked on a stalled position for more than half a day.
    minimal_roi = {
        "0": 0.06,
        "30": 0.04,
        "60": 0.025,
        "120": 0.015,
        "180": 0.008,
        "360": 0.004,
        "720": 0.001,
    }

    # custom_stoploss provides the dynamic trailing; disable the static trailer.
    trailing_stop = False
    use_custom_stoploss = True

    # ── Hyperopt parameter space (defaults tuned for 4-6 trades/week) ──────────
    buy_stoch_max = IntParameter(10, 30, default=25, space="buy")
    buy_volume_ratio = DecimalParameter(1.0, 2.5, default=1.3, space="buy")
    buy_rsi_1h_min = IntParameter(35, 50, default=38, space="buy")
    buy_rsi_1h_max = IntParameter(62, 78, default=70, space="buy")
    buy_rsi_4h_max = IntParameter(65, 80, default=72, space="buy")
    buy_adx_min = IntParameter(14, 28, default=18, space="buy")

    sell_stoch_min = IntParameter(70, 90, default=80, space="sell")
    sell_rsi_1h_exit = IntParameter(38, 52, default=45, space="sell")

    atr_stop_multiplier = DecimalParameter(1.0, 2.5, default=1.5, space="stoploss")
    atr_trail_multiplier = DecimalParameter(0.2, 0.8, default=0.5, space="stoploss")

    # GapHunter watchlist gate. Loaded lazily and cached per process; refreshed
    # when the file's mtime changes (the daily cycle rewrites it each morning).
    _watchlist_cache: dict = {}
    _watchlist_mtime: float = 0.0

    # -------------------------------------------------------------------------
    # Informative data
    # -------------------------------------------------------------------------
    def informative_pairs(self):
        # BTC macro regime anchor (Layer 1). Per-pair 1h/4h come from @informative.
        return [(REGIME_ANCHOR, "1d"), (REGIME_ANCHOR, "4h")]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Layer 2 — 4h trend structure.
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Layer 3 — 1h trend confirmation.
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Layer 4 — 5m entry-timing indicators on the base timeframe.
        stoch = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
        dataframe["fastk"] = stoch["fastk"]
        dataframe["fastd"] = stoch["fastd"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["volume_mean"] = dataframe["volume"].rolling(24).mean()

        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]

        # Layer 1 — merge BTC 1d regime. EMA200(1d): is BTC in a bull regime?
        dataframe = self._merge_btc_regime(dataframe, metadata)
        return dataframe

    def _merge_btc_regime(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Attach a forward-filled ``btc_below_ema200_1d`` flag to the 5m frame."""
        dataframe["btc_below_ema200_1d"] = False  # safe default (fail-open to trading)
        if not self.dp:
            return dataframe
        try:
            btc = self.dp.get_pair_dataframe(REGIME_ANCHOR, "1d")
            if btc is None or btc.empty:
                return dataframe
            btc = btc.copy()
            btc["ema200"] = ta.EMA(btc, timeperiod=200)
            btc["btc_below_ema200"] = btc["close"] < btc["ema200"]
            merged = merge_informative_pair(
                dataframe, btc[["date", "btc_below_ema200"]], self.timeframe, "1d", ffill=True
            )
            # merge_informative_pair suffixes the column with the informative tf.
            col = "btc_below_ema200_1d"
            if col in merged.columns:
                merged[col] = merged[col].fillna(False).astype(bool)
                return merged
        except Exception as exc:  # noqa: BLE001 — never let regime merge crash analysis
            logger.warning("BTC regime merge failed (%s): trading without the gate.", exc)
        return dataframe

    # -------------------------------------------------------------------------
    # Entry logic (Layers 1-4)
    # -------------------------------------------------------------------------
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0

        # Layer 2 — 4h trend healthy.
        trend_ok_4h = (
            (dataframe["ema21_4h"] > dataframe["ema50_4h"])
            & (dataframe["ema50_4h"] > dataframe["ema200_4h"])
            & (dataframe["close"] > dataframe["ema21_4h"])
            & (dataframe["adx_4h"] > self.buy_adx_min.value)
            & (dataframe["rsi_4h"] < self.buy_rsi_4h_max.value)
        )

        # Layer 3 — 1h trend healthy.
        trend_ok_1h = (
            (dataframe["close"] > dataframe["ema21_1h"])
            & (dataframe["ema21_1h"] > dataframe["ema50_1h"])
            & (dataframe["rsi_1h"].between(self.buy_rsi_1h_min.value, self.buy_rsi_1h_max.value))
            & (dataframe["macd_1h"] > dataframe["macdsignal_1h"])
        )

        # Layer 4 — 5m sniper trigger.
        entry_5m = (
            (dataframe["fastk"] < self.buy_stoch_max.value)
            & (qtpylib.crossed_above(dataframe["fastk"], dataframe["fastd"]))
            & (dataframe["volume"] > dataframe["volume_mean"] * self.buy_volume_ratio.value)
            & (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["rsi"] > 25)
        )

        dataframe.loc[trend_ok_4h & trend_ok_1h & entry_5m, "enter_long"] = 1

        # Layer 1 — hard regime gate: no longs while BTC is below its 200d EMA.
        if "btc_below_ema200_1d" in dataframe.columns:
            dataframe.loc[dataframe["btc_below_ema200_1d"], "enter_long"] = 0

        return dataframe

    # -------------------------------------------------------------------------
    # Layer 7 — Exit signals
    # -------------------------------------------------------------------------
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe.loc[
            # EXIT 1: momentum exhaustion (5m overbought reversal)
            (
                (dataframe["fastk"] > self.sell_stoch_min.value)
                & qtpylib.crossed_below(dataframe["fastk"], dataframe["fastd"])
            )
            # EXIT 2: 1h trend structure break
            | (
                (dataframe["close"] < dataframe["ema21_1h"])
                & (dataframe["rsi_1h"] < self.sell_rsi_1h_exit.value)
            )
            # EXIT 3: volume dry-up while price rolls over (distribution)
            | (
                (dataframe["volume"] < dataframe["volume_mean"] * 0.5)
                & (dataframe["close"] < dataframe["close"].shift(3))
            ),
            "exit_long",
        ] = 1
        return dataframe

    # -------------------------------------------------------------------------
    # Layer 6 — ATR dynamic stop
    # -------------------------------------------------------------------------
    def custom_stoploss(
        self, pair: str, trade, current_time, current_rate, current_profit, **kwargs
    ) -> float:
        """Volatility-aware stop. Returns a stoploss as a ratio relative to current_rate.

        Baseline: ``atr_stop_multiplier`` x ATR below entry. As profit builds we
        trail tighter (0.5x ATR from +2%, 0.3x ATR from +5%) and always return the
        *tightest* (highest) of the candidate stops so locked-in gains are kept.
        """
        atr = self._latest_atr(pair)
        if atr is None or atr <= 0 or not trade.open_rate:
            return self.stoploss

        entry_price = trade.open_rate

        # Initial stop expressed relative to the current rate (freqtrade convention).
        initial_stop_price = entry_price - (self.atr_stop_multiplier.value * atr)
        candidates = [(initial_stop_price - current_rate) / current_rate]

        # Tightest branch first so the +5% lock can override the +2% lock.
        if current_profit >= 0.05:
            tight = current_rate - (0.3 * atr)
            candidates.append((tight - current_rate) / current_rate)
        elif current_profit >= 0.02:
            trail = current_rate - (self.atr_trail_multiplier.value * atr)
            candidates.append((trail - current_rate) / current_rate)

        # Highest value = stop closest to price = tightest protection.
        return max(candidates)

    def _latest_atr(self, pair: str) -> Optional[float]:
        """Most recent 1h ATR, read from the analyzed (merged) dataframe."""
        if not self.dp:
            return None
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and not df.empty and "atr_1h" in df.columns:
                val = df["atr_1h"].iloc[-1]
                return float(val) if pd.notna(val) else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("ATR lookup failed for %s: %s", pair, exc)
        return None

    # -------------------------------------------------------------------------
    # Layer 5 — AI pre-entry veto
    # -------------------------------------------------------------------------
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time,
        entry_tag: Optional[str] = None,
        side: str = "long",
        **kwargs,
    ) -> bool:
        """Block any entry not backed by today's GapHunter watchlist.

        Fail-open on scanner errors (return True) so a missing/corrupt file never
        silently halts trading — the technical layers still gate every entry.
        During backtest/hyperopt there is no daily scan, so the gate is skipped.
        """
        if self.dp and self.dp.runmode.value in ("backtest", "hyperopt", "plot"):
            return True

        try:
            watchlist = self._load_watchlist()
            if not watchlist:
                logger.warning("[%s] No GapHunter watchlist available — allowing (fail-open).", pair)
                return True

            entry = next((s for s in watchlist if s.get("pair") == pair), None)
            if entry is None:
                logger.warning("[%s] Not on today's GapHunter watchlist. Blocking.", pair)
                return False

            score = entry.get("total_score", 0)
            min_score = self._watchlist_cache.get("min_score", 60)
            if score < min_score:
                logger.warning("[%s] Gap score %.1f < %.0f. Blocking.", pair, score, min_score)
                return False

            # Fast technical sanity check: avoid 1h-overextended chases.
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and not df.empty and "rsi_1h" in df.columns:
                latest_rsi = df["rsi_1h"].iloc[-1]
                if pd.notna(latest_rsi) and latest_rsi > self.buy_rsi_1h_max.value + 2:
                    logger.warning("[%s] 1h RSI overextended (%.1f). Blocking.", pair, latest_rsi)
                    return False

            logger.info("[%s] Trade confirmed. Gap score %.1f/100.", pair, score)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("confirm_trade_entry error for %s: %s", pair, exc)
            return True  # fail-open

    def _load_watchlist(self) -> list:
        """Load and cache today's watchlist, refreshing when the file changes."""
        if not WATCHLIST_PATH.exists():
            return []
        mtime = WATCHLIST_PATH.stat().st_mtime
        if mtime != self._watchlist_mtime or not self._watchlist_cache:
            try:
                data = json.loads(WATCHLIST_PATH.read_text())
                self._watchlist_cache = data
                self._watchlist_mtime = mtime
                logger.info(
                    "Loaded GapHunter watchlist (%d pairs) from %s",
                    len(data.get("watchlist", [])),
                    WATCHLIST_PATH,
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to read watchlist %s: %s", WATCHLIST_PATH, exc)
                return []
        return self._watchlist_cache.get("watchlist", [])
