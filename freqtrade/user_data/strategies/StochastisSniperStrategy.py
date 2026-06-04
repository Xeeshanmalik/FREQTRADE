# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file

import numpy as np
import pandas as pd
from pandas import DataFrame
from freqtrade.strategy import (IStrategy, IntParameter, DecimalParameter)
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib

class StochastisSniperStrategy(IStrategy):
    """
    Stochastic Sniper
    - Only buys when the trend is clearly UP (EMA alignment).
    - Only buys when momentum FLIPS up (StochRSI Crossover).
    - Prevents "Catching Falling Knives".
    """
    INTERFACE_VERSION = 3
    timeframe = '5m'

    # -------------------------------------------------------------------------
    # STOPLOSS & ROI
    # -------------------------------------------------------------------------
    # Tighter stoploss to kill bad trades early.
    stoploss = -0.08  # 8% Hard Stop

    # Dynamic ROI: We aim for 6%, but we secure profit if it stalls.
    minimal_roi = {
        "0": 0.06,      # Target: 6%
        "20": 0.03,     # If held 20 mins, accept 3%
        "40": 0.015,    # If held 40 mins, accept 1.5%
        "60": 0.005     # If held 1 hour, exit at break-even (0.5%)
    }

    # Trailing Stop: Lock in profit once we hit 1.5% gains
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1. Trend Filter: EMA 100 and EMA 200
        dataframe['ema_100'] = ta.EMA(dataframe, timeperiod=100)
        dataframe['ema_200'] = ta.EMA(dataframe, timeperiod=200)

        # 2. Momentum Sniper: Stochastic RSI
        # This detects the exact moment the dip turns into a rally.
        stoch_rsi = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
        dataframe['fastk'] = stoch_rsi['fastk']
        dataframe['fastd'] = stoch_rsi['fastd']

        # 3. Volume Check
        dataframe['volume_mean'] = dataframe['volume'].rolling(window=24).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # CONDITION 1: Market must be in an Uptrend
                # Price is above EMA 100, and EMA 100 is above EMA 200.
                (dataframe['close'] > dataframe['ema_100']) &
                (dataframe['ema_100'] > dataframe['ema_200']) &

                # CONDITION 2: The Sniper Trigger (Crossover)
                # We do NOT buy just because it's low.
                # We buy when the Fast K line CROSSES ABOVE the Slow D line.
                (dataframe['fastk'] < 20) &  # Must be oversold
                (qtpylib.crossed_above(dataframe['fastk'], dataframe['fastd'])) &
                
                # CONDITION 3: Volume Spike
                # Ensure people are actually buying this move.
                (dataframe['volume'] > dataframe['volume_mean'])
            ),
            'enter_long'] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # EXIT 1: Overbought (Take Profit)
                # If RSI gets too hot (>80) and crosses down, we sell.
                (dataframe['fastk'] > 80) &
                (qtpylib.crossed_below(dataframe['fastk'], dataframe['fastd']))
            ) |
            (
                # EXIT 2: Trend Crash (Safety)
                # If price falls below the long-term EMA 200, the trend is dead. Get out.
                (dataframe['close'] < dataframe['ema_200'])
            ),
            'exit_long'] = 1

        return dataframe