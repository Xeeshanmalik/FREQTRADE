# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
# isort: skip_file

import os
import json
import logging
import datetime
from typing import Dict, Optional
from datetime import timezone

import numpy as np
import pandas as pd
from pandas import DataFrame

import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
from freqtrade.strategy import IStrategy

# Google Generative AI
import google.generativeai as genai

logger = logging.getLogger(__name__)

class GeminiHybridStrategy(IStrategy):
    """
    Gemini Hybrid "Daily Sniper" Strategy
    
    1. Checks for a "BUY" signal from Gemini Pro ONLY TWICE A DAY (00:00 UTC and 12:00 UTC)
    2. Does NOT use Gemini for selling.
    3. Exits are strictly managed by dynamic ROI, Stoploss, and Trailing Stops.
    """
    INTERFACE_VERSION = 3
    
    # We use a 1h timeframe as it gives Gemini good macro context without drowning in noise.
    timeframe = '1h'

    # --- Traditional Sell Parameters to Maximize Profit Offline ---
    stoploss = -0.10  # 10% hard stoploss in case Gemini is very wrong
    
    # Dynamic ROI to secure profits
    minimal_roi = {
        "0": 0.20,     # If it pumps 20% instantly, take the money and run
        "240": 0.10,   # If it takes 4 hours (240 mins), take 10%
        "720": 0.05,   # If it takes 12 hours, take 5%
        "1440": 0.02   # If we hold for a full day, accept a modest 2% to free up capital
    }

    # Trailing Stop: Let profits run but lock them in if momentum breaks
    trailing_stop = True
    trailing_stop_positive = 0.02           # 2% trailing stop
    trailing_stop_positive_offset = 0.05    # Only active after 5% profit

    # --- Strategy State ---
    
    # Cache the AI responses so Freqtrade doesn't constantly spam Gemini on every tick
    # Format: { "BTC/USDT": {"last_checked": <datetime>, "signal": "BUY" } }
    gemini_cache: Dict[str, Dict] = {}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        
        # Initialize Gemini API
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY environment variable not found! Strategy will not generate buy signals.")
        else:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel('gemini-1.5-pro-latest')
            logger.info("Gemini Hybrid Strategy initialized successfully.")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Calculates simple indicators so we can feed the numbers to Gemini.
        """
        # MACD
        macd = ta.MACD(dataframe)
        dataframe['macd'] = macd['macd']
        dataframe['macdsignal'] = macd['macdsignal']
        
        # RSI
        dataframe['rsi'] = ta.RSI(dataframe)
        
        # Volume Mean
        dataframe['volume_mean'] = dataframe['volume'].rolling(window=24).mean()

        return dataframe

    def get_gemini_verdict(self, pair: str, dataframe: DataFrame) -> str:
        """
        Formats recent data and asks Gemini if it's a good time to buy.
        Returns: "BUY", "SELL", or "HOLD" (Though we only act on BUY)
        """
        try:
            if not os.environ.get("GEMINI_API_KEY"):
                return "HOLD"
                
            # Take the last 5 candles to give Gemini context
            recent_data = dataframe.tail(5).copy()
            
            # Format the data into a readable string for the LLM
            prompt = f"You are an expert crypto trader. Analyze the following 1-hour candle data for {pair}.\n\n"
            
            for index, row in recent_data.iterrows():
                prompt += f"- Close: {row['close']}, Volume: {row['volume']}, RSI: {row['rsi']:.2f}, MACD: {row['macd']:.2f}\n"
                
            prompt += "\nBased strictly on this data, is this a strong BUY setup? Respond with exactly one word: 'BUY' if it's a strong macro setup, or 'HOLD' if it's not clear or bearish. Do not provide any other text."

            # Call Gemini
            response = self.model.generate_content(prompt)
            verdict = response.text.strip().upper()
            
            logger.info(f"Gemini Verdict for {pair}: {verdict}")
            
            if "BUY" in verdict:
                return "BUY"
                
            return "HOLD"
            
        except Exception as e:
            logger.error(f"Error querying Gemini API: {e}")
            return "HOLD"

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Determine entry signals based on our 2x-daily Gemini check.
        """
        dataframe['enter_long'] = 0
        pair = metadata['pair']
        
        # Only run this logic if we are essentially at the end of the dataframe (Live/Dry-Run checking the real market)
        # We DO NOT want this firing during historical backtesting.
        if self.dp and self.dp.runmode.value in ('live', 'dry_run'):
            # Check the current clock from the dataframe's latest candle
            latest_date = dataframe['date'].iloc[-1]
            
            # We only evaluate at 00:00 UTC and 12:00 UTC (Hour == 0 or Hour == 12)
            # And we make sure we haven't already checked this specific hour recently
            if latest_date.hour in [0, 12]:
                
                # Check cache to see if we already asked Gemini this hour
                cache_entry = self.gemini_cache.get(pair)
                needs_check = True
                
                if cache_entry:
                    last_checked = cache_entry['last_checked']
                    # If we already checked during this specific 12-hour window today, don't check again
                    if last_checked.date() == latest_date.date() and last_checked.hour == latest_date.hour:
                        needs_check = False
                
                if needs_check:
                    logger.info(f"[{pair}] Reached 12-hour Gemini evaluation window. Querying API...")
                    verdict = self.get_gemini_verdict(pair, dataframe)
                    
                    # Store in cache
                    self.gemini_cache[pair] = {
                        "last_checked": latest_date,
                        "signal": verdict
                    }
                    
                # Apply the signal if it's a BUY
                # We apply it to the last candle (iloc[-1]) so Freqtrade buys immediately
                current_cache = self.gemini_cache.get(pair)
                if current_cache and current_cache["signal"] == "BUY":
                     # Only keep the buy signal active for the specific exact hour candle we checked on
                     if current_cache['last_checked'] == latest_date:
                        dataframe.loc[dataframe.index[-1], 'enter_long'] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        We do NOT use Gemini for exits.
        We let Freqtrade's ROI and Trailing Stop logic handle maximizing profits.
        """
        dataframe['exit_long'] = 0
        # No programmatic exits here; let `minimal_roi` and `trailing_stop` do the work offline.
        return dataframe
