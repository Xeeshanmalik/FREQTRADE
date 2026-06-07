# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file
"""
ShortPrecisionStrategy — the bearish mirror of UltraPrecisionStrategy.

UltraPrecision is long-only and stands aside in bear regimes (BTC < 200d EMA),
so it earns nothing while the market falls. This strategy trades exactly that
regime: it SHORTS confirmed downtrends on USDT perpetuals when BTC is bearish.

    Layer 1: Regime gate    — shorts ONLY while BTC < EMA200(1d) (bear market)
    Layer 2: 4h downtrend   — EMA stack bearish + ADX + RSI floor (not oversold)
    Layer 3: 1h downtrend   — EMA stack bearish + RSI band + MACD < signal
    Layer 4: 5m short timing — StochRSI cross DOWN from overbought + volume + BB ceiling
    Exit: fast ROI on the drop + trend-reversal-up signal; ATR/static stop.

Futures (isolated), 1x leverage for the first honest read of the edge (no
amplification until validated). Research/backtest only — NOT wired to live.
"""
import logging
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
)

logger = logging.getLogger(__name__)
REGIME_ANCHOR = "BTC/USDT:USDT"


class ShortPrecisionStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    can_short = True
    startup_candle_count = 200

    # ROI = profit ratio (direction-agnostic in freqtrade). Shorts mean-revert /
    # squeeze fast, so harvest the drop briskly.
    minimal_roi = {"0": 0.05, "30": 0.03, "60": 0.02, "120": 0.012, "360": 0.006}
    stoploss = -0.06
    # Trailing stop is the exit-leak repair (validated 2026-06-07): captures the
    # favourable extreme of a winning short instead of riding it back to a 1h-flip
    # loss. Backtest bear-window: PF 1.43 -> 1.47, +130% -> +170%, DD 12.8 -> 14.7%.
    trailing_stop = True
    trailing_stop_positive = 0.012
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True
    use_custom_stoploss = False

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 3},
            {"method": "StoplossGuard", "lookback_period_candles": 288,
             "trade_limit": 2, "stop_duration_candles": 288, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period_candles": 1440,
             "trade_limit": 3, "stop_duration_candles": 720, "max_allowed_drawdown": 0.20},
        ]

    # ── Entry params (bearish mirror) ──────────────────────────────────────────
    sell_stoch_min = IntParameter(70, 90, default=75, space="sell")   # 5m overbought to fade
    sell_volume_ratio = DecimalParameter(1.0, 2.5, default=1.3, space="sell")
    sell_rsi_1h_min = IntParameter(25, 45, default=30, space="sell")  # don't short already-oversold
    sell_rsi_1h_max = IntParameter(55, 70, default=62, space="sell")
    sell_adx_min = IntParameter(14, 28, default=18, space="sell")
    # cover when 1h flips back up
    buy_rsi_1h_exit = IntParameter(48, 62, default=55, space="buy")

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, side, **kwargs) -> float:
        return 1.0  # unlevered first — validate the edge before amplifying

    def informative_pairs(self):
        # MUST declare candle_type="futures": without it freqtrade requests SPOT
        # BTC 1d data (not loaded in a futures backtest), get_pair_dataframe returns
        # empty, and the regime flag silently defaults to False — which is fail-OPEN
        # for longs but fail-CLOSED for shorts, so it blocked 100% of entries.
        return [(REGIME_ANCHOR, "1d", "futures")]

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        stoch = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
        dataframe["fastk"] = stoch["fastk"]
        dataframe["fastd"] = stoch["fastd"]
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["volume_mean"] = dataframe["volume"].rolling(24).mean()
        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_upper"] = bb["upper"]
        dataframe["bb_mid"] = bb["mid"]
        dataframe = self._merge_btc_regime(dataframe, metadata)
        return dataframe

    def _merge_btc_regime(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Attach a ``btc_below_ema200_1d`` flag (shorts require it True).

        Uses an explicit ``merge_asof`` rather than ``merge_informative_pair``:
        in this freqtrade build the helper failed to suffix the flag column in a
        futures backtest (the column went missing), silently leaving the regime
        gate False and blocking every short. merge_asof is deterministic and we
        shift each daily flag forward one day so a 1d candle is only used AFTER it
        closes (no look-ahead)."""
        dataframe["btc_below_ema200_1d"] = False
        if not self.dp:
            return dataframe
        try:
            btc = self.dp.get_pair_dataframe(REGIME_ANCHOR, "1d")
            if btc is None or btc.empty:
                return dataframe
            btc = btc.copy()
            btc["ema200"] = ta.EMA(btc, timeperiod=200)
            flag = pd.DataFrame({
                # available from the next day's open → no look-ahead
                "date": btc["date"] + pd.Timedelta(days=1),
                "btc_flag": (btc["close"] < btc["ema200"]).fillna(False).astype(bool),
            })
            merged = pd.merge_asof(
                dataframe.sort_values("date"),
                flag.sort_values("date"),
                on="date", direction="backward",
            )
            merged["btc_below_ema200_1d"] = merged["btc_flag"].fillna(False).astype(bool)
            return merged.drop(columns=["btc_flag"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("BTC regime merge failed (%s): no shorts without it.", exc)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0

        # Layer 2 — 4h downtrend healthy.
        down_4h = (
            (dataframe["ema21_4h"] < dataframe["ema50_4h"])
            & (dataframe["ema50_4h"] < dataframe["ema200_4h"])
            & (dataframe["close"] < dataframe["ema21_4h"])
            & (dataframe["adx_4h"] > self.sell_adx_min.value)
            & (dataframe["rsi_4h"] > 28)  # not already capitulated
        )
        # Layer 3 — 1h downtrend healthy.
        down_1h = (
            (dataframe["close"] < dataframe["ema21_1h"])
            & (dataframe["ema21_1h"] < dataframe["ema50_1h"])
            & (dataframe["rsi_1h"].between(self.sell_rsi_1h_min.value, self.sell_rsi_1h_max.value))
            & (dataframe["macd_1h"] < dataframe["macdsignal_1h"])
        )
        # Entry = confirmed 4h+1h downtrend. The original Layer-4 overbought-fade
        # 5m trigger fired ZERO times in the full bear window (validated 2026-06-07):
        # fading momentum is the wrong instinct here, same lesson as the long side
        # (mean-reversion failed). This is trend-following — short while structure
        # holds; the trailing stop + 1h-flip exit handle timing on the way out.
        dataframe.loc[down_4h & down_1h, "enter_short"] = 1

        # Layer 1 — regime gate: shorts only while BTC is below its 200d EMA.
        if "btc_below_ema200_1d" in dataframe.columns:
            dataframe.loc[~dataframe["btc_below_ema200_1d"], "enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        # Cover when the 1h structure flips back up (thesis broken).
        dataframe.loc[
            (dataframe["close"] > dataframe["ema21_1h"])
            & (dataframe["rsi_1h"] > self.buy_rsi_1h_exit.value),
            "exit_short",
        ] = 1
        return dataframe
