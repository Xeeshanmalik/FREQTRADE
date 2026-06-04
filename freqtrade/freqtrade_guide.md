# Freqtrade: The "Zero to Hero" Guide

This guide explains how Freqtrade works under the hood and how to read the dashboard, assuming absolutely zero prior knowledge.

---

## 1. How the Bot "Thinks" (The Loop)
Freqtrade is a **loop**. Every few seconds (usually every 5 seconds), it wakes up and does this exact sequence:

1.  **Fetch Data**
    *   **What it does:** Asks the exchange for Open/High/Low/Close data.
    *   **Where in Code:**
        *   **Which Coins?**: `config.json` → `pair_whitelist` (e.g., "XRP/USDT").
        *   **Timeframe**: `UserStrategy.py` → `timeframe = "5m"` (Line 49).

2.  **Calculate Indicators**
    *   **What it does:** Runs math on the candles.
    *   **Where in Code:** `UserStrategy.py` → `populate_indicators` function.
        ```python
        def populate_indicators(self, dataframe, metadata):
            # RSI (Momentum)
            dataframe["rsi"] = ta.RSI(dataframe)
            # EMA 200 (Trend) - NEW!
            dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        ```

3.  **Check for Buy Signals**
    *   **What it does:** Decides if we should enter a trade.
    *   **Where in Code:** `UserStrategy.py` → `populate_entry_trend` function.
        ```python
        def populate_entry_trend(self, dataframe, metadata):
            # Logic: If Price is in Uptrend (Above EMA 200) AND Oversold...
            dataframe.loc[
                (dataframe["close"] > dataframe["ema200"]) &  # Trend Guard
                (dataframe["rsi"] < 27) ...                   # Optimized Signal
                "enter_long"] = 1  # Action: BUY
        ```

4.  **Check for Sell Signals**
    *   **What it does:** Decides if we should exit a trade.
    *   **Where in Code:**
        *   **Strategy Exit**: `UserStrategy.py` → `populate_exit_trend` (Logic: RSI > 80).
        *   **Emergency Exit**: `UserStrategy.py` → `stoploss = -0.027` (Fixed -2.7% loss).
        *   **Profit Take**: `UserStrategy.py` → `minimal_roi` (Dynamic profit taking).

5.  **Sleep**
    *   **What it does:** Waits before checking again.
    *   **Where in Code:** `config.json` → `internals` → `process_throttle_secs` (Default: 5).

### Key Concept: "Dry Run" (Paper Trading)
You are currently in **Dry Run** mode.
- **The Data is Real**: The bot watches *real* live prices from Binance.
- **The Money is Fake**: The bot has a "virtual wallet" (e.g., 1000 USDT). When it "buys", it just writes down "I bought BTC at $50,000" in its database. It sends **zero** money to the exchange.
- **Why?**: This lets you test strategies safely. If you lose money here, you learn a lesson for free.

---

## 2. Simply Explained: The Strategy File
Your strategy (`UserStrategy.py`) is the "Brain". It answers two questions:

1.  **When to Enter?** (`populate_entry_trend`)
    - You defined: "Enter when the coin is in an **Uptrend** (Price > EMA 200) AND is temporarily **Oversold** (RSI < 27)."
    - *Why?*: Buying in an uptrend is safer. We buy the "dip" in a rising market.
2.  **When to Exit?** (`populate_exit_trend` + ROI + Stoploss)
    - You defined: "Exit when the coin is **Overbought** (RSI > 80)."
    - **Stoploss**: "If price drops 2.7%, SELL IMMEDIATELY." (Tuned by Hyperopt to minimize losses).
    - **ROI (Return on Investment)**: "Take profit if we hit +4% quickly, or even +11% if it takes longer."

---

## 3. Decoding the Dashboard (127.0.0.1)

When you look at the dashboard, here is what matters:

### Top Bar
- **status**: Should be `running`.
- **Dry Run**: Should be `True` (Green check). **Always check this before putting in real money!**
- **Balance**: Shows your current available Free/Used stake currency (e.g., `USDT: 1000`).

### "Balance" Tab
Click the **Balance** icon (usually a wallet symbol) in the sidebar to see a full breakdown of every coin you hold, including those currently in open trades.

### "Trade status" / "Profit/Loss" Widget
This is your scoreboard.
- **Total Profit**: All your closed trades combined.
    - *Green*: You are making money.
    - *Red*: You are losing money.
- **Daily Stats**: How much you made today.

### ❓ How to Know if You Are Winning
It is common to see your numeric **Balance** drop (e.g., 1000 -> 800) when the bot buys something. **This is NOT a loss yet!**
- **Free Balance**: Money sitting in your wallet doing nothing (e.g., 800 USDT).
- **Locked Balance**: Money currently invested in crypto (e.g., 200 USDT worth of XRP).
- **Total Equity**: Free + Locked. **This is your REAL score.**

**To check if you are profitable:**
1.  Look at **Total Profit** (Realized gains from closed trades).
2.  Look at **Open Profit %** (Unrealized gains from active trades).


### "Open Trades" Tab (The Action)
This shows what you own *right now*.
- **Pair**: What coin you hold (e.g., `XRP/USDT`).
- **Amount**: The **Number of Coins** you bought (e.g., 1120 XRP). This is NOT your dollar value.
- **Open Profit %**: The most important number.
    - `+2.5%`: You are winning.
    - `-5.0%`: You are currently losing on this specific trade (holding "bags").
- **Current Price**: The live price right now.
- **Open Price**: The price you bought at.

### "Logs" (The Matrix)
This is the raw stream of the bot's thoughts.
- **"Searching for USDT pairs..."**: The bot is awake and downloading price data. This is normal heartbeat.
- **"Buy Signal for ETH/USDT..."**: The bot found a match for your strategy!
- **"Exiting Trade..."**: The bot is selling.

---

## 4. Common Questions

**Q: Why isn't it buying anything?**
A: Your strategy is strict! `RSI < 27` in an `Uptrend` is a specific condition. If the market is going sideways or down (below EMA 200), the bot will refuse to buy. This is **good**—it prevents losing money in bad markets.

**Q: What is `candles`?**
A: A candle is a summary of price for a specific time (e.g., 5 minutes).
- **Green Candle**: Price went UP in those 5 mins.
- **Red Candle**: Price went DOWN in those 5 mins.
- Your bot makes decisions based on the *history* of these candles.

**Q: Can I change settings while it runs?**
A: No. If you change code, you must **Restart** the bot for it to "reload" the new brain.
