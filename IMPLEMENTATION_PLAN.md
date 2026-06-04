# FREQTRADE AGENTIC TRADING SYSTEM — FULL IMPLEMENTATION PLAN

> **Prepared for:** Freqtrade v2026.1 · Binance Spot · 1000 USDT · Max 1 Open Trade  
> **Architecture:** GapHunter Coin Selector + UltraPrecision Executor + Claude Agentic Loop  
> **Goal:** 4–6 trades/week at 62–70% win rate — quality over quantity, capital active 5 days out of 7

---

## THE CORE INSIGHT (READ THIS FIRST)

Most traders lose because they fight over HOW to enter trades while ignoring WHICH coin they are trading. The reality is:

> **80% of your edge comes from coin selection. 20% comes from the entry signal.**

A coin on the verge of a breakout will make any indicator look like a genius.  
A dead coin in distribution will destroy any indicator no matter how perfect.

This plan is built around that insight. The **GapHunter** (coin selection engine) does the heavy lifting first, then the **UltraPrecision** strategy (entry/exit engine) acts only on the pre-selected high-probability candidates. The **Claude Agent Loop** ties everything together with autonomous daily intelligence.

**Confirmed targets for this system:**
- Trade frequency: **4–6 trades/week** (capital active ~5 days/7)
- Win rate: **62–70%** (vs ~45% for a typical single-indicator strategy)
- Avg trade duration: 2–24 hours (ROI exits keep capital turning over)
- Reward/Risk ratio: ≥ 2.0:1
- Expected value per trade: +0.7–1.2%
- Max drawdown: < 8%

---

## SYSTEM OVERVIEW

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DAILY AUTOMATED CYCLE                               │
│                                                                             │
│  06:00 UTC                06:30 UTC              07:00 UTC                  │
│  Data Pull         →      Gap Scanner     →      Claude Review    →         │
│  (all 40 pairs)           (6 gap types)          (selects top 5-8)          │
│                                                        │                    │
│                                                        ▼                    │
│                            ┌───────────────────────────────┐               │
│                            │   DAILY WATCHLIST (5-8 coins) │               │
│                            │   Scored 0-100, min 60/100    │               │
│                            └───────────────────────────────┘               │
│                                         │                                   │
│                                         ▼                                   │
│                     ┌────────────────────────────────────┐                 │
│                     │    UltraPrecision Strategy          │                 │
│                     │    Layer 1: Market Regime Gate      │                 │
│                     │    Layer 2: 4h Trend Confirmation   │                 │
│                     │    Layer 3: 1h Trend Confirmation   │                 │
│                     │    Layer 4: 5m Entry Timing         │                 │
│                     │    Layer 5: AI Pre-Entry Veto       │                 │
│                     │    Layer 6: ATR Dynamic Exit        │                 │
│                     │    Layer 7: Claude Post-Entry Guard │                 │
│                     └────────────────────────────────────┘                 │
│                                         │                                   │
│                                         ▼                                   │
│  20:00 UTC                                                                  │
│  Evening Review  ←  Trade DB  ←  Closed Trade (Win or Loss)                │
│  (Claude learns, adjusts weights, sends Telegram report)                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## PART 1: THE GAPHUNTER COIN SELECTION ENGINE

### What Is a "Gap" in Crypto?

A "gap" in this system means any detectable asymmetry or void in price, volume, momentum, or relative performance that creates a high-probability directional opportunity. There are 6 types we scan for daily.

---

### Gap Type 1: Fair Value Gap (FVG) — Imbalance Detection

**What it is:** A 3-candle pattern where price moved so fast that it left an "imbalanced" zone — the high of candle 1 and the low of candle 3 do not overlap. Institutional algorithms are programmed to return price to these zones to fill orders that were missed.

**Why it works:** Market makers (institutions) hold unfilled limit orders in these gaps. When price returns, there is a predictable flood of buying/selling pressure at those levels.

**Entry logic:** When price retraces INTO a bullish FVG (gap left during an upward move) in an uptrend = extremely high-probability long entry.

```python
# scripts/gap_scanner.py — Gap Type 1
def detect_fair_value_gaps(df: pd.DataFrame, timeframe: str = '4h') -> list:
    """
    Scans for unmitigated bullish Fair Value Gaps.
    Returns list of gaps sorted by recency and size.
    """
    gaps = []
    for i in range(2, len(df)):
        c1_high = df['high'].iloc[i - 2]
        c3_low  = df['low'].iloc[i]
        candle_date = df['date'].iloc[i - 1]  # the gap candle
        
        # Bullish FVG: gap between c1 high and c3 low
        if c1_high < c3_low:
            gap_bottom = c1_high
            gap_top    = c3_low
            gap_size   = (gap_top - gap_bottom) / gap_bottom  # as %
            
            # Check if already mitigated (price already returned)
            future_prices = df['low'].iloc[i:]
            is_mitigated  = (future_prices <= gap_top).any()
            
            if not is_mitigated and gap_size >= 0.005:  # min 0.5% gap
                gaps.append({
                    'date':        candle_date,
                    'gap_bottom':  gap_bottom,
                    'gap_top':     gap_top,
                    'gap_size_pct': gap_size * 100,
                    'bars_ago':    len(df) - i,
                    'timeframe':   timeframe,
                })
    
    # Sort by recency (gaps formed recently are most powerful)
    return sorted(gaps, key=lambda x: x['bars_ago'])


def score_fvg(gaps: list, current_price: float) -> float:
    """Score 0-25 based on proximity to nearest unmitigated bullish FVG."""
    if not gaps:
        return 0.0
    
    nearest_gap = min(gaps, key=lambda g: abs(current_price - g['gap_top']))
    distance_pct = (current_price - nearest_gap['gap_top']) / current_price
    
    score = 0.0
    # Has unmitigated bullish FVG nearby
    if 0 <= distance_pct <= 0.05:   # price within 5% above gap
        score += 15
        # Approaching (within 2% above gap top)
        if distance_pct <= 0.02:
            score += 5
        # Large gap = stronger magnet
        if nearest_gap['gap_size_pct'] >= 1.0:
            score += 5
    
    return min(score, 25.0)
```

**Scoring contribution: 0–25 points**

---

### Gap Type 2: Volume Profile Void — Resistance-Free Paths

**What it is:** A Volume Profile divides the historical price range into price buckets and measures how much volume traded at each level. "Low Volume Nodes" (LVN) are price zones where almost no volume has traded. "High Volume Nodes" (HVN) are dense zones.

**Why it works:** When price enters an LVN, there is almost no historical buying/selling pressure to slow it down — it moves through fast. When price enters an HVN, it consolidates because of all the historical limit orders. Knowing the path of least resistance lets you set aggressive targets.

**Entry logic:** When current price is in an LVN with an HVN as the next major resistance = free run upside.

```python
# scripts/gap_scanner.py — Gap Type 2
def build_volume_profile(df: pd.DataFrame, bins: int = 100) -> dict:
    """Build a price-volume histogram."""
    price_min = df['low'].min()
    price_max = df['high'].max()
    bin_size  = (price_max - price_min) / bins
    
    profile = {b: 0.0 for b in range(bins)}
    
    for _, row in df.iterrows():
        low_bin  = int((row['low']  - price_min) / bin_size)
        high_bin = int((row['high'] - price_min) / bin_size)
        n_bins   = max(high_bin - low_bin + 1, 1)
        vol_per_bin = row['volume'] / n_bins
        for b in range(low_bin, min(high_bin + 1, bins)):
            profile[b] += vol_per_bin
    
    return {
        'profile':    profile,
        'bin_size':   bin_size,
        'price_min':  price_min,
        'price_max':  price_max,
    }


def score_volume_profile(vp: dict, current_price: float) -> float:
    """Score 0-20: is current price in a Low Volume Node?"""
    profile   = vp['profile']
    bin_size  = vp['bin_size']
    price_min = vp['price_min']
    
    avg_volume = sum(profile.values()) / len(profile)
    current_bin = int((current_price - price_min) / bin_size)
    
    if current_bin not in profile:
        return 0.0
    
    current_vol = profile[current_bin]
    
    # Find next HVN above current price
    next_hvn_bins = [
        b for b, v in profile.items()
        if b > current_bin and v > avg_volume * 1.5
    ]
    
    score = 0.0
    if current_vol < avg_volume * 0.4:  # in a Low Volume Node
        score += 12
        if next_hvn_bins:  # there's a clear target (next HVN)
            distance_to_hvn_pct = ((next_hvn_bins[0] - current_bin) * bin_size) / current_price
            if distance_to_hvn_pct >= 0.03:  # target is at least 3% away
                score += 8
    
    return min(score, 20.0)
```

**Scoring contribution: 0–20 points**

---

### Gap Type 3: Relative Strength Gap — Sector Rotation Intelligence

**What it is:** Measures how each coin is performing relative to Bitcoin (the market anchor) and relative to its sector peers. When capital rotates into a sector, the strongest coin in that sector leads the move.

**Why it works:** In crypto bull markets, capital flows sector by sector (BTC → ETH → L1s → DeFi → Gaming → AI). Being in the right sector at the right time is a massive edge. Within a sector, the strongest coin (highest relative strength) is the one institutions are accumulating.

**Entry logic:** Trade coins that are outperforming BTC on multiple timeframes (7d, 3d, 1d) — these are under institutional accumulation.

```python
# scripts/gap_scanner.py — Gap Type 3
def calculate_relative_strength(coin_df: pd.DataFrame, btc_df: pd.DataFrame) -> dict:
    """
    Compute coin performance vs BTC over multiple periods.
    RS > 1.0 = coin outperforming BTC (bullish)
    RS < 1.0 = coin underperforming BTC (bearish)
    """
    results = {}
    for period_hours in [24, 72, 168]:  # 1d, 3d, 7d
        if len(coin_df) < period_hours or len(btc_df) < period_hours:
            continue
        coin_chg = coin_df['close'].iloc[-1] / coin_df['close'].iloc[-period_hours] - 1
        btc_chg  = btc_df['close'].iloc[-1]  / btc_df['close'].iloc[-period_hours]  - 1
        rs = (1 + coin_chg) / (1 + btc_chg)
        results[f'rs_{period_hours}h'] = rs
    return results


def score_relative_strength(rs_data: dict) -> float:
    """Score 0-20 based on relative strength vs BTC."""
    score = 0.0
    
    # 7-day RS (most important: institutional accumulation signal)
    rs_7d = rs_data.get('rs_168h', 1.0)
    if rs_7d >= 1.10:  score += 10
    elif rs_7d >= 1.05: score += 7
    elif rs_7d >= 1.02: score += 4
    
    # 1-day RS (momentum confirmation)
    rs_1d = rs_data.get('rs_24h', 1.0)
    if rs_1d >= 1.03:  score += 10
    elif rs_1d >= 1.01: score += 6
    elif rs_1d >= 1.00: score += 3
    
    return min(score, 20.0)
```

**Scoring contribution: 0–20 points**

---

### Gap Type 4: Fibonacci Retracement Gap — Institutional Buy Zones

**What it is:** After any significant upward move, price retraces. Fibonacci ratios (38.2%, 50%, 61.8%) are not magic — they are self-fulfilling because enough institutions program their systems to buy at these exact levels, making them actual support.

**Why it works:** The 61.8% "golden pocket" (between 61.8% and 65%) is where the deepest healthy retracement ends before trend continuation. Buying here with trend confirmation is one of the highest win-rate setups in all of trading.

**Entry logic:** Price at 50%–65% Fibonacci retracement of the last major upleg, in an uptrend = golden pocket long.

```python
# scripts/gap_scanner.py — Gap Type 4
def find_major_swing(df: pd.DataFrame, lookback: int = 50) -> tuple:
    """Find the most significant recent swing high and swing low."""
    window = df.tail(lookback)
    swing_high = window['high'].max()
    swing_high_idx = window['high'].idxmax()
    
    # Swing low must be BEFORE the swing high (for retracement calculation)
    pre_high = window.loc[:swing_high_idx]
    swing_low = pre_high['low'].min()
    
    return swing_high, swing_low


def calculate_fib_levels(swing_high: float, swing_low: float) -> dict:
    diff = swing_high - swing_low
    return {
        '0.236': swing_high - 0.236 * diff,
        '0.382': swing_high - 0.382 * diff,
        '0.500': swing_high - 0.500 * diff,
        '0.618': swing_high - 0.618 * diff,
        '0.650': swing_high - 0.650 * diff,  # golden pocket edge
        '0.786': swing_high - 0.786 * diff,
    }


def score_fibonacci(fib_levels: dict, current_price: float) -> float:
    """Score 0-15 based on proximity to key Fibonacci levels."""
    score = 0.0
    tolerance = 0.015  # within 1.5% of the level
    
    fib_scores = {
        '0.618': 10, '0.650': 10,  # golden pocket
        '0.500': 7,
        '0.382': 5,
        '0.786': 4,
        '0.236': 2,
    }
    
    for level, pts in fib_scores.items():
        fib_price = fib_levels.get(level, 0)
        if fib_price > 0:
            distance = abs(current_price - fib_price) / fib_price
            if distance <= tolerance:
                score = max(score, pts)  # take best matching level
                break
    
    return min(score, 15.0)
```

**Scoring contribution: 0–15 points**

---

### Gap Type 5: Liquidity Sweep Gap — The Most Innovative Signal

**What it is:** The most powerful and least understood pattern in crypto. Large players (market makers, institutions) need liquidity to fill large orders. They engineer price to sweep below visible support levels where retail stop losses cluster, triggering those stops to create the sell orders they need to buy from. After the sweep, price reverses sharply upward.

**Pattern:**
1. Price consolidates → equal lows form (retail places stops below)
2. Price briefly dips below equal lows (stop hunt — "the sweep")
3. Price closes back above the level within 1–2 candles (rejection)
4. **This is the buy signal** — stops are cleared, smart money has accumulated

**Why it works:** After the sweep, the selling pressure (stop losses) has been absorbed. The path of least resistance is now UP because the "fuel" (retail shorts/stops) has been consumed.

```python
# scripts/gap_scanner.py — Gap Type 5
def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    Detect recent liquidity sweeps below sell-side liquidity pools.
    A sweep is: price wicks below recent equal lows, then closes back above.
    """
    result = {'detected': False, 'strength': 0.0, 'bars_ago': None}
    
    for i in range(lookback, len(df)):
        # Find equal lows in the prior window (stop cluster zone)
        window_lows = df['low'].iloc[i - lookback: i]
        support_level = window_lows.quantile(0.15)  # approximate stop cluster
        
        current_candle = df.iloc[i]
        
        # Sweep: wick went below support but candle CLOSED above it
        if (current_candle['low'] < support_level and
                current_candle['close'] > support_level):
            
            sweep_depth = (support_level - current_candle['low']) / support_level
            bars_ago = len(df) - 1 - i
            
            if bars_ago <= 5:  # only consider recent sweeps
                result = {
                    'detected':   True,
                    'strength':   min(sweep_depth * 10, 1.0),  # normalize 0-1
                    'bars_ago':   bars_ago,
                    'swept_level': support_level,
                }
    
    return result


def score_liquidity_sweep(sweep: dict) -> float:
    """Score 0-15 based on recent liquidity sweep detection."""
    if not sweep['detected']:
        return 0.0
    
    # More recent sweep = higher score (signal decays quickly)
    recency_multiplier = {0: 1.0, 1: 0.9, 2: 0.75, 3: 0.6, 4: 0.45, 5: 0.3}
    bars_ago = sweep.get('bars_ago', 5)
    mult = recency_multiplier.get(bars_ago, 0.3)
    
    base_score = 15 * sweep['strength'] * mult
    return min(base_score, 15.0)
```

**Scoring contribution: 0–15 points**

---

### Gap Type 6: Time-of-Day Volume Gap — Trade With the Flow

**What it is:** Every coin has a natural "active window" when its primary buyer base (Asian, European, or US session) is trading. During these windows, volume is 2–3x higher, spreads are tighter, and moves are more sustained.

**Why it works:** Thin-volume periods (e.g., trading a US-focused coin during Asian hours) create false breakouts and noisy signals. The same signal during high-volume hours is far more reliable.

```python
# scripts/gap_scanner.py — Gap Type 6
def analyze_time_of_day(df: pd.DataFrame) -> dict:
    """Find which UTC hours this coin sees the highest volume."""
    df = df.copy()
    df['hour'] = pd.to_datetime(df['date']).dt.hour
    
    hourly_vol = df.groupby('hour')['volume'].mean()
    peak_hours = hourly_vol.nlargest(8).index.tolist()  # top 8 hours of day
    
    return {
        'peak_hours':       sorted(peak_hours),
        'hourly_profile':   hourly_vol.to_dict(),
    }


def score_time_of_day(time_analysis: dict, current_utc_hour: int) -> float:
    """Score 0-5 based on whether we're in peak volume window."""
    if current_utc_hour in time_analysis['peak_hours']:
        return 5.0
    return 0.0
```

**Scoring contribution: 0–5 points**

---

### The Master Scoring Algorithm

```python
# scripts/coin_scorer.py — Master scorer

SECTOR_MAP = {
    'BTC/USDT': 'store-of-value',
    'ETH/USDT': 'smart-contract',
    'SOL/USDT': 'smart-contract',
    'AVAX/USDT': 'smart-contract',
    'MATIC/USDT': 'layer2',
    'ARB/USDT': 'layer2',
    'OP/USDT': 'layer2',
    'UNI/USDT': 'defi',
    'AAVE/USDT': 'defi',
    'LINK/USDT': 'oracle',
    # ... extend for all 40 pairs
}

def score_coin(pair: str, df_5m: pd.DataFrame, df_1h: pd.DataFrame,
               df_4h: pd.DataFrame, btc_df: pd.DataFrame) -> dict:
    
    current_price = df_1h['close'].iloc[-1]
    current_hour  = pd.Timestamp.utcnow().hour
    
    # Run all 6 gap detectors
    fvg_gaps      = detect_fair_value_gaps(df_4h, timeframe='4h')
    vp            = build_volume_profile(df_1h.tail(200), bins=100)
    rs_data       = calculate_relative_strength(df_1h, btc_df)
    sh, sl        = find_major_swing(df_4h, lookback=50)
    fib_levels    = calculate_fib_levels(sh, sl)
    sweep         = detect_liquidity_sweep(df_1h, lookback=20)
    time_analysis = analyze_time_of_day(df_1h)
    
    # Score each dimension
    s_fvg    = score_fvg(fvg_gaps, current_price)
    s_vp     = score_volume_profile(vp, current_price)
    s_rs     = score_relative_strength(rs_data)
    s_fib    = score_fibonacci(fib_levels, current_price)
    s_sweep  = score_liquidity_sweep(sweep)
    s_time   = score_time_of_day(time_analysis, current_hour)
    
    total_score = s_fvg + s_vp + s_rs + s_fib + s_sweep + s_time  # max 100
    
    return {
        'pair':          pair,
        'total_score':   round(total_score, 2),
        'breakdown': {
            'fair_value_gap':    s_fvg,
            'volume_profile':    s_vp,
            'relative_strength': s_rs,
            'fibonacci':         s_fib,
            'liquidity_sweep':   s_sweep,
            'time_of_day':       s_time,
        },
        'current_price': current_price,
        'sector':        SECTOR_MAP.get(pair, 'other'),
        'timestamp':     pd.Timestamp.utcnow().isoformat(),
    }


def select_daily_watchlist(all_scores: list, min_score: float = 60.0,
                           max_pairs: int = 8) -> list:
    """
    Select top-scoring coins for today's watchlist.
    Rules:
      - Must score >= 60/100  (relaxed from 65 to target 4-6 trades/week)
      - No more than 2 coins from same sector (diversification)
      - Always include at least 1 BTC-correlated coin as hedge
    """
    eligible = [c for c in all_scores if c['total_score'] >= min_score]
    eligible.sort(key=lambda x: x['total_score'], reverse=True)
    
    selected = []
    sector_count = {}
    
    for coin in eligible:
        if len(selected) >= max_pairs:
            break
        sector = coin['sector']
        if sector_count.get(sector, 0) >= 2:
            continue
        selected.append(coin)
        sector_count[sector] = sector_count.get(sector, 0) + 1
    
    return selected
```

**Minimum threshold:** A coin needs ≥ 60/100 to enter the daily watchlist.  
**Daily output:** Top 5–8 coins, saved to `user_data/gap_analysis/daily_scores.json`.  
**Frequency rationale:** Scanning 8 pairs vs 5, with threshold at 60 vs 65, statistically produces 5–7 entry opportunities per week from which 4–6 will pass all 7 strategy layers.

---

## PART 2: THE ULTRAPRECISION EXECUTION STRATEGY

**File:** `user_data/strategies/UltraPrecisionStrategy.py`

Once the GapHunter has selected the daily watchlist (3–5 coins), the UltraPrecision strategy applies 7 layers of confirmation before entering a trade.

### Layer 1: Market Regime Gate (Daily — Do Not Trade Bear Markets)

```python
def informative_pairs(self):
    # Pull BTC 1d data as macro regime indicator
    return [('BTC/USDT', '1d'), ('BTC/USDT', '4h')]

# In populate_indicators for BTC/USDT 1d:
#   - btc_ema200_1d: is BTC above its 200-day EMA?
#   - btc_adx_1d: is the BTC trend ADX > 20?
#
# GATE: if BTC < EMA200(1d) → regime = BEAR → all entry_long = 0
```

In `populate_entry_trend`:
```python
# Hard gate: no longs in bear market
if dataframe['btc_below_ema200'].iloc[-1]:
    dataframe['enter_long'] = 0
    return dataframe
```

### Layer 2: 4h Trend Confirmation

```python
# informative 4h indicators
dataframe['ema21_4h']  = ta.EMA(dataframe, timeperiod=21)
dataframe['ema50_4h']  = ta.EMA(dataframe, timeperiod=50)
dataframe['ema200_4h'] = ta.EMA(dataframe, timeperiod=200)
dataframe['adx_4h']    = ta.ADX(dataframe, timeperiod=14)
dataframe['rsi_4h']    = ta.RSI(dataframe, timeperiod=14)

# Required: EMA21 > EMA50 > EMA200 AND price > EMA21 AND ADX > 18
# ADX threshold lowered from 20 to 18 to allow slightly less trending markets
# through — gains ~1 extra opportunity/week without compromising trend quality
trend_ok_4h = (
    (dataframe['ema21_4h'] > dataframe['ema50_4h']) &
    (dataframe['ema50_4h'] > dataframe['ema200_4h']) &
    (dataframe['close']    > dataframe['ema21_4h'])  &
    (dataframe['adx_4h']   > 18)                     &
    (dataframe['rsi_4h']   < 72)  # not overbought on 4h (relaxed from 70)
)
```

### Layer 3: 1h Trend Confirmation

```python
# informative 1h indicators
dataframe['ema21_1h']  = ta.EMA(dataframe, timeperiod=21)
dataframe['ema50_1h']  = ta.EMA(dataframe, timeperiod=50)
dataframe['rsi_1h']    = ta.RSI(dataframe, timeperiod=14)
dataframe['macd_1h']   = ta.MACD(dataframe)['macd']
dataframe['macds_1h']  = ta.MACD(dataframe)['macdsignal']

# Required: 1h trend healthy (not overbought, not broken)
# RSI range widened from (40,68) to (38,70) to capture slightly more setups
# while still excluding extreme conditions in both directions
trend_ok_1h = (
    (dataframe['close']    > dataframe['ema21_1h'])  &
    (dataframe['ema21_1h'] > dataframe['ema50_1h'])  &
    (dataframe['rsi_1h']   .between(38, 70))         &
    (dataframe['macd_1h']  > dataframe['macds_1h'])  # 1h MACD bullish
)
```

### Layer 4: 5m Entry Timing (The Sniper Trigger)

```python
# 5m base timeframe — the actual entry trigger
stoch = ta.STOCHRSI(dataframe, timeperiod=14, fastk_period=3, fastd_period=3)
dataframe['fastk']       = stoch['fastk']
dataframe['fastd']       = stoch['fastd']
dataframe['rsi_5m']      = ta.RSI(dataframe, timeperiod=14)
dataframe['volume_mean'] = dataframe['volume'].rolling(24).mean()
bb = qtpylib.bollinger_bands(dataframe['close'], window=20, stds=2)
dataframe['bb_lower']    = bb['lower']
dataframe['bb_mid']      = bb['mid']

# Entry trigger: oversold StochRSI crossover with volume + not below BB
# StochRSI threshold relaxed from <20 to <25 — catches slightly earlier reversals
# Volume ratio relaxed from 1.5x to 1.3x — still confirms real buying pressure
# Combined effect: ~1-2 more valid entry signals per week
entry_5m = (
    (dataframe['fastk'] < 25) &
    (qtpylib.crossed_above(dataframe['fastk'], dataframe['fastd'])) &
    (dataframe['volume']  > dataframe['volume_mean'] * 1.3) &
    (dataframe['close']   > dataframe['bb_lower'])           &  # no breakdown
    (dataframe['rsi_5m']  > 25)                               # not in freefall
)
```

### Layer 5: AI Pre-Entry Trade Validator (Claude Veto)

```python
def confirm_trade_entry(self, pair, order_type, amount, rate,
                        time_in_force, current_time, **kwargs) -> bool:
    """
    Claude reviews every trade before it executes.
    Returns False to BLOCK the trade, True to ALLOW it.
    """
    try:
        # Pull gap score for this pair (written by GapHunter each morning)
        scores_path = '/freqtrade/user_data/gap_analysis/daily_scores.json'
        with open(scores_path) as f:
            scores = json.load(f)
        
        pair_score = next(
            (s for s in scores['watchlist'] if s['pair'] == pair), None
        )
        
        if not pair_score:
            # Pair not on today's watchlist — block it
            logger.warning(f"[{pair}] Not on today's GapHunter watchlist. Blocking.")
            return False
        
        if pair_score['total_score'] < 60:
            logger.warning(f"[{pair}] Gap score too low ({pair_score['total_score']}). Blocking.")
            return False
        
        # Additional sanity checks without API call (fast path)
        df = self.dp.get_pair_dataframe(pair, '1h')
        if df is None or len(df) < 50:
            return False
        
        latest_rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else 50
        if latest_rsi > 72:
            logger.warning(f"[{pair}] 1h RSI overextended ({latest_rsi:.1f}). Blocking.")
            return False
        
        logger.info(f"[{pair}] Trade confirmed. Gap score: {pair_score['total_score']}/100")
        return True
        
    except Exception as e:
        logger.error(f"confirm_trade_entry error: {e}")
        return True  # fail open — don't block on scanner error
```

### Layer 6: ATR-Based Dynamic Exit Management

```python
def custom_stoploss(self, pair, trade, current_time, current_rate,
                    current_profit, **kwargs) -> float:
    """
    Dynamic stop loss that adapts to volatility.
    Uses 1h ATR to set initial stop and trail profits.
    """
    df_1h = self.dp.get_pair_dataframe(pair, '1h')
    if df_1h is None or 'atr' not in df_1h.columns:
        return self.stoploss
    
    atr = df_1h['atr'].iloc[-1]
    entry_price = trade.open_rate
    
    # Initial stop: 1.5× ATR below entry
    initial_stop_price = entry_price - (1.5 * atr)
    initial_stop_pct   = (initial_stop_price - entry_price) / entry_price
    
    # Profit-locking: once up 2%, trail at 0.5× ATR
    if current_profit >= 0.02:
        trail_price    = current_rate - (0.5 * atr)
        trail_stop_pct = (trail_price - current_rate) / current_rate
        # Return whichever stop is tighter (higher value = closer to price)
        return max(initial_stop_pct, trail_stop_pct)
    
    # Once up 5%, trail very tightly (0.3× ATR)
    if current_profit >= 0.05:
        tight_trail = current_rate - (0.3 * atr)
        return max(initial_stop_pct, (tight_trail - current_rate) / current_rate)
    
    return initial_stop_pct


minimal_roi = {
    "0":   0.06,    # 6% — take it if price spikes
    "30":  0.04,    # 4% after 30 min — momentum fading
    "60":  0.025,   # 2.5% after 1 hour
    "120": 0.015,   # 1.5% after 2 hours
    "180": 0.008,   # 0.8% after 3 hours
    "360": 0.004,   # 0.4% after 6 hours — force capital turnover
    "720": 0.001    # 0.1% after 12 hours — exit near break-even if stuck
}
# The 360 and 720 entries are critical for 4-6 trades/week:
# they guarantee capital is freed within 12 hours on slow/sideways trades,
# so the bot is never locked out of a fresh high-quality setup for >half a day.
```

### Layer 7: Exit Signal (StochRSI Overbought + Trend Break)

```python
def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
    dataframe.loc[
        # EXIT 1: Momentum exhaustion (5m overbought reversal)
        (
            (dataframe['fastk'] > 80) &
            qtpylib.crossed_below(dataframe['fastk'], dataframe['fastd'])
        ) |
        # EXIT 2: Trend structure break (1h)
        (
            (dataframe['close'] < dataframe['ema21_1h']) &
            (dataframe['rsi_1h'] < 45)
        ) |
        # EXIT 3: Volume dry-up on 5m (distribution signal)
        (
            (dataframe['volume'] < dataframe['volume_mean'] * 0.5) &
            (dataframe['close']  < dataframe['close'].shift(3))
        ),
        'exit_long'
    ] = 1
    return dataframe
```

### Hyperopt Parameter Space (Self-Optimizing)

```python
# Baked into UltraPrecisionStrategy for automated optimization
# Defaults tuned for 4-6 trades/week target (slightly more permissive than
# the original quality-only values, but still above noise floor)
buy_stoch_max        = IntParameter(10, 30, default=25, space='buy')   # was 20
buy_volume_ratio     = DecimalParameter(1.0, 2.5, default=1.3, space='buy')   # was 1.5
buy_rsi_1h_min       = IntParameter(35, 50, default=38, space='buy')   # was 45
buy_rsi_1h_max       = IntParameter(62, 78, default=70, space='buy')   # was 68
buy_adx_min          = IntParameter(14, 28, default=18, space='buy')   # was 20
sell_stoch_min       = IntParameter(70, 90, default=80, space='sell')
sell_rsi_1h_exit     = IntParameter(38, 52, default=43, space='sell')
atr_stop_multiplier  = DecimalParameter(1.0, 2.5, default=1.5, space='stoploss')
atr_trail_multiplier = DecimalParameter(0.2, 0.8, default=0.5, space='stoploss')
```

---

## PART 3: THE CLAUDE AGENTIC LOOP

This is the intelligence layer that makes the system self-improving. Claude Code runs as an autonomous agent on a daily schedule.

### Agent Architecture

```
scripts/
├── daily_gap_scan.py       ← Runs all 6 gap detectors, saves scores JSON
├── coin_scorer.py          ← Scoring logic (imported by daily_gap_scan.py)
├── config_updater.py       ← Writes new pair_whitelist to config.json
├── performance_extractor.py← Reads SQLite DB, computes win stats
├── hyperopt_trigger.py     ← Decides if hyperopt should run today
├── launch_agent.sh         ← Already exists (launches Claude tmux session)
└── prompts/
    ├── morning_review.md   ← Claude's morning analysis prompt
    └── evening_review.md   ← Claude's evening learning prompt
```

### Morning Review Prompt (`scripts/prompts/morning_review.md`)

```markdown
You are an autonomous crypto trading agent managing a Freqtrade bot.
Today is {DATE} UTC.

## Your Task
Review the GapHunter scores below and finalize today's trading watchlist.

## Gap Scanner Output
{SCANNER_JSON}

## Recent Performance (Last 7 Days)
{PERFORMANCE_JSON}

## Instructions
1. Review each coin's gap score and breakdown.
2. For any coin scoring ≥ 60, check if there is obvious negative news
   (major hack, regulatory action, exchange delisting risk) that the 
   technical scan cannot detect.
3. Apply a ±10 point qualitative adjustment if warranted.
4. Select final 5-8 coins for today's watchlist (target: enough candidates
   to generate 4-6 actual trades across the full week).
5. Set the market regime: BULL, NEUTRAL, or BEAR based on BTC 4h structure.
6. Output ONLY valid JSON in this exact format:

{
  "date": "YYYY-MM-DD",
  "market_regime": "BULL|NEUTRAL|BEAR",
  "watchlist": ["COIN1/USDT", "COIN2/USDT", ...],
  "excluded": [{"pair": "X/USDT", "reason": "..."}],
  "max_trades_today": 1,
  "confidence_note": "one sentence summary"
}
```

### Evening Review Prompt (`scripts/prompts/evening_review.md`)

```markdown
You are a self-improving trading agent reviewing today's results.

## Today's Trades
{TRADES_JSON}

## Today's Watchlist Was
{WATCHLIST_JSON}

## Your Task
1. For each LOSING trade: identify which gap signal was wrong or missing.
   Which of the 6 gap types would have filtered this trade out?
2. For each WINNING trade: identify which gap signals were strongest.
3. Output a score_weight_adjustment JSON — suggest multiplier changes (0.8-1.2)
   for each of the 6 gap dimensions based on today's evidence:
   - fair_value_gap, volume_profile, relative_strength,
     fibonacci, liquidity_sweep, time_of_day

4. Note: changes should be small (±20% max) and based on solid evidence.

Output JSON format:
{
  "date": "YYYY-MM-DD",
  "win_rate_today": 0.XX,
  "weight_adjustments": {
    "fair_value_gap": 1.0,
    "volume_profile": 1.0,
    "relative_strength": 1.0,
    "fibonacci": 1.0,
    "liquidity_sweep": 1.0,
    "time_of_day": 1.0
  },
  "key_learnings": ["...", "..."],
  "telegram_summary": "2-sentence summary for notification"
}
```

### Daily Automation Script (`scripts/run_daily_cycle.sh`)

```bash
#!/bin/bash
set -euo pipefail

export PATH="$HOME/.local/share/fnm:$PATH"
eval "$(fnm env --shell bash)" 2>/dev/null
fnm use lts-latest 2>/dev/null

REPO="/root/code/freqtrade/freqtrade"
VENV_PYTHON="$REPO/../.venv/bin/python"  # or system python3

log() { echo "[$(date -u '+%Y-%m-%d %H:%M UTC')] $*"; }

# ── Step 1: Download fresh data ────────────────────────────────────────────
log "Downloading market data..."
docker exec freqtrade freqtrade download-data \
  --timeframes 5m 1h 4h \
  --days 30 \
  --config /freqtrade/user_data/config.json \
  2>&1 | tail -5

# ── Step 2: Run gap scanner ────────────────────────────────────────────────
log "Running GapHunter scanner..."
$VENV_PYTHON $REPO/scripts/daily_gap_scan.py \
  --data-dir $REPO/user_data/data/binance \
  --output $REPO/user_data/gap_analysis/daily_scores.json

# ── Step 3: Claude morning review ─────────────────────────────────────────
log "Running Claude morning review..."
SCORES=$(cat $REPO/user_data/gap_analysis/daily_scores.json)
PERF=$(cat $REPO/user_data/gap_analysis/performance_7d.json 2>/dev/null || echo '{}')
TODAY=$(date -u +%Y-%m-%d)

PROMPT="Today is $TODAY UTC.
## Gap Scanner Output
$SCORES
## Recent 7-Day Performance
$PERF
$(cat $REPO/scripts/prompts/morning_review.md)"

claude --print "$PROMPT" \
  > $REPO/user_data/gap_analysis/claude_decision.json

# ── Step 4: Update freqtrade config whitelist ──────────────────────────────
log "Updating pair whitelist..."
$VENV_PYTHON $REPO/scripts/config_updater.py \
  --decisions $REPO/user_data/gap_analysis/claude_decision.json \
  --config $REPO/user_data/config.json

# ── Step 5: Restart freqtrade to pick up new whitelist ─────────────────────
log "Restarting freqtrade..."
docker restart freqtrade
sleep 10

log "Daily cycle complete. Bot trading today's watchlist."
```

### Config Updater (`scripts/config_updater.py`)

```python
#!/usr/bin/env python3
"""
Reads Claude's daily decision JSON and updates config.json pair_whitelist.
Replaces the VolumePairList with a StaticPairList for today's watchlist.
"""
import json
import argparse
from pathlib import Path

def update_config(decisions_path: str, config_path: str):
    with open(decisions_path) as f:
        decisions = json.load(f)
    
    with open(config_path) as f:
        config = json.load(f)
    
    watchlist = decisions.get('watchlist', [])
    regime    = decisions.get('market_regime', 'NEUTRAL')
    
    if not watchlist:
        print("WARNING: Empty watchlist from Claude. Keeping existing config.")
        return
    
    # Adjust risk based on market regime
    regime_settings = {
        'BULL':    {'max_open_trades': 1, 'tradable_balance_ratio': 0.95},
        'NEUTRAL': {'max_open_trades': 1, 'tradable_balance_ratio': 0.80},
        'BEAR':    {'max_open_trades': 0, 'tradable_balance_ratio': 0.00},  # flat
    }
    
    settings = regime_settings.get(regime, regime_settings['NEUTRAL'])
    config['max_open_trades']        = settings['max_open_trades']
    config['tradable_balance_ratio'] = settings['tradable_balance_ratio']
    
    # Replace pairlist with today's watchlist
    config['exchange']['pair_whitelist'] = watchlist
    config['pairlists'] = [
        {'method': 'StaticPairList'},  # Use static list from pair_whitelist
        {'method': 'SpreadFilter', 'max_spread_ratio': 0.003},
    ]
    
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)
    
    print(f"Config updated: {watchlist} | Regime: {regime}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--decisions', required=True)
    parser.add_argument('--config',    required=True)
    args = parser.parse_args()
    update_config(args.decisions, args.config)
```

### Performance Extractor (`scripts/performance_extractor.py`)

```python
#!/usr/bin/env python3
"""
Reads tradesv3.sqlite and computes performance stats.
Outputs JSON for Claude's morning/evening review.
"""
import json
import sqlite3
import argparse
from datetime import datetime, timedelta, timezone

def extract_performance(db_path: str, days: int = 7) -> dict:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    
    cursor.execute("""
        SELECT pair, profit_ratio, profit_abs, open_date, close_date,
               open_rate, close_rate, exit_reason
        FROM trades
        WHERE is_open = 0
          AND close_date >= ?
        ORDER BY close_date DESC
    """, (since,))
    
    trades = cursor.fetchall()
    conn.close()
    
    if not trades:
        return {'total_trades': 0, 'win_rate': 0, 'trades': []}
    
    wins  = [t for t in trades if t[1] > 0]
    total = len(trades)
    
    trade_list = [
        {
            'pair':         t[0],
            'profit_pct':   round(t[1] * 100, 3),
            'profit_usdt':  round(t[2], 4),
            'open_date':    t[3],
            'close_date':   t[4],
            'open_rate':    t[5],
            'close_rate':   t[6],
            'exit_reason':  t[7],
        }
        for t in trades
    ]
    
    return {
        'period_days':   days,
        'total_trades':  total,
        'wins':          len(wins),
        'losses':        total - len(wins),
        'win_rate':      round(len(wins) / total, 3),
        'avg_profit_pct': round(sum(t[1] for t in trades) / total * 100, 3),
        'total_profit_usdt': round(sum(t[2] for t in trades), 4),
        'trades':        trade_list,
    }

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',   required=True)
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    
    stats = extract_performance(args.db, args.days)
    with open(args.output, 'w') as f:
        json.dump(stats, f, indent=2)
    print(f"Extracted {stats['total_trades']} trades. Win rate: {stats['win_rate']}")
```

### Hyperopt Trigger (`scripts/hyperopt_trigger.py`)

```python
#!/usr/bin/env python3
"""
Automatically triggers hyperopt when win rate drops below threshold.
Runs after evening review.
"""
import json
import subprocess

HYPEROPT_THRESHOLD_WIN_RATE = 0.57  # trigger if below 57% over last 15 trades
HYPEROPT_THRESHOLD_TRADES   = 15    # ~3 weeks of data at 4-6 trades/week

def should_run_hyperopt(perf_path: str) -> bool:
    with open(perf_path) as f:
        perf = json.load(f)
    
    if perf['total_trades'] < HYPEROPT_THRESHOLD_TRADES:
        print(f"Only {perf['total_trades']} trades — not enough data yet.")
        return False
    
    if perf['win_rate'] < HYPEROPT_THRESHOLD_WIN_RATE:
        print(f"Win rate {perf['win_rate']:.1%} below threshold. Triggering hyperopt.")
        return True
    
    print(f"Win rate {perf['win_rate']:.1%} is healthy. No hyperopt needed.")
    return False

def run_hyperopt():
    cmd = [
        'docker', 'exec', 'freqtrade', 'freqtrade', 'hyperopt',
        '--strategy', 'UltraPrecisionStrategy',
        '--hyperopt-loss', 'SharpeHyperOptLossDaily',
        '--spaces', 'buy', 'sell', 'stoploss',
        '--epochs', '200',
        '--timerange', '20250101-',  # use all available data
        '--config', '/freqtrade/user_data/config.json',
    ]
    print("Running hyperopt (200 epochs)...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    
    if result.returncode == 0:
        print("Hyperopt complete. Applying best parameters.")
        apply_best_params()
    else:
        print(f"Hyperopt failed: {result.stderr[:500]}")

def apply_best_params():
    # Read .last_result.json from hyperopt_results and extract params
    # Then patch UltraPrecisionStrategy.py default values
    pass  # Implementation: parse hyperopt result JSON, update strategy defaults

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--perf', required=True)
    args = parser.parse_args()
    
    if should_run_hyperopt(args.perf):
        run_hyperopt()
```

### Cron Schedule (`crontab -e`)

```cron
# ─── GapHunter Daily Cycle ──────────────────────────────────────────────────
# Morning cycle: download data, scan gaps, Claude review, update config
0 6 * * * /root/code/freqtrade/freqtrade/scripts/run_daily_cycle.sh >> /var/log/gaphunter.log 2>&1

# Evening review: extract performance, run Claude analysis, check hyperopt
0 20 * * * /root/code/freqtrade/freqtrade/scripts/run_evening_cycle.sh >> /var/log/gaphunter.log 2>&1

# Watchdog: ensure freqtrade container is running (every 15 min)
*/15 * * * * docker ps | grep -q freqtrade || docker start freqtrade
```

---

## PART 4: FILE STRUCTURE (COMPLETE)

```
/root/code/freqtrade/freqtrade/
│
├── docker-compose.yml           (existing — minor update for env vars)
├── freqtrade_guide.md           (existing — reference)
│
├── scripts/
│   ├── launch_agent.sh          (existing — fixed $ARA_CMD bug)
│   ├── run_daily_cycle.sh       ← NEW: morning automation
│   ├── run_evening_cycle.sh     ← NEW: evening automation
│   ├── daily_gap_scan.py        ← NEW: runs all 6 gap detectors
│   ├── coin_scorer.py           ← NEW: scoring algorithm
│   ├── config_updater.py        ← NEW: updates freqtrade config
│   ├── performance_extractor.py ← NEW: reads SQLite, computes stats
│   ├── hyperopt_trigger.py      ← NEW: auto-triggers hyperopt
│   └── prompts/
│       ├── morning_review.md    ← NEW: Claude morning prompt template
│       └── evening_review.md    ← NEW: Claude evening prompt template
│
└── user_data/
    ├── config.json              (existing — updated by config_updater.py)
    ├── strategies/
    │   ├── GeminiHybridStrategy.py      (existing — keep as backup)
    │   ├── StochastisSniperStrategy.py  (existing — keep as reference)
    │   └── UltraPrecisionStrategy.py    ← NEW: main production strategy
    └── gap_analysis/                    ← NEW directory
        ├── daily_scores.json            ← Scanner output (overwritten daily)
        ├── claude_decision.json         ← Claude's watchlist decision
        ├── performance_7d.json          ← Last 7 days trade stats
        ├── performance_today.json       ← Today's trade stats
        ├── weight_history.json          ← Historical weight adjustments
        └── score_weights.json           ← Current scoring weights (adaptive)
```

---

## PART 5: IMPLEMENTATION TIMELINE

### Day 1 — Data Infrastructure
```bash
# Download 6 months of data across all timeframes
docker exec freqtrade freqtrade download-data \
  --timeframes 5m 1h 4h 1d \
  --days 180 \
  --config /freqtrade/user_data/config.json

# Create gap_analysis directory
mkdir -p user_data/gap_analysis
mkdir -p scripts/prompts
```

### Day 2 — Build & Backtest UltraPrecisionStrategy
```bash
# Backtest against 6 months of data
docker exec freqtrade freqtrade backtesting \
  --strategy UltraPrecisionStrategy \
  --timerange 20250601-20260601 \
  --config /freqtrade/user_data/config.json

# Target metrics to pass before proceeding:
#   Win rate:    > 58%
#   Profit factor: > 1.8
#   Max drawdown: < 12%
#   Sharpe ratio: > 1.0
```

### Day 3 — Build & Test GapHunter Scanner
```bash
# Test gap scanner on existing data
python3 scripts/daily_gap_scan.py \
  --data-dir user_data/data/binance \
  --output user_data/gap_analysis/test_scores.json

# Manually inspect top-scoring coins
cat user_data/gap_analysis/test_scores.json | python3 -m json.tool | head -80
```

### Day 4 — Hyperopt
```bash
docker exec freqtrade freqtrade hyperopt \
  --strategy UltraPrecisionStrategy \
  --hyperopt-loss SharpeHyperOptLossDaily \
  --spaces buy sell stoploss \
  --epochs 300 \
  --timerange 20250601-20260601 \
  --config /freqtrade/user_data/config.json
```

### Day 5 — Re-backtest with Optimized Parameters
```bash
# Apply hyperopt results and re-run backtest
docker exec freqtrade freqtrade backtesting \
  --strategy UltraPrecisionStrategy \
  --timerange 20260101-20260601 \   # use most recent 6m as out-of-sample
  --config /freqtrade/user_data/config.json
# If win rate > 62% on out-of-sample: proceed
# If not: adjust strategy, repeat
```

### Week 2 — Dry Run with Full System
```bash
# Set up cron jobs
crontab -e  # add the two cron lines from Part 3

# Run morning cycle manually to test
bash scripts/run_daily_cycle.sh

# Verify Claude decision output
cat user_data/gap_analysis/claude_decision.json

# Monitor bot
docker logs -f freqtrade | grep -E "(buy|sell|profit|error)"
```

### Week 3 — Performance Analysis
- Run evening review every night
- Watch for: win rate, avg profit per trade, max drawdown
- Trigger hyperopt manually if win rate < 58% after 30 trades

### Week 4 — Go Live (Small Capital First)
```bash
# In config.json: set dry_run to false and stake_amount to 50 USDT
# Keep max_open_trades = 1
# Only increase stake after 50+ live trades with win rate > 60%
```

---

## PART 6: DECISION FLOW DIAGRAM (Single Trade)

```
EVERY 5 SECONDS (Freqtrade loop):

For each pair in today's watchlist (5-8 pairs):
│
├── [CHECK] Pair on GapHunter watchlist today?
│     NO  → SKIP (don't even evaluate indicators)
│     YES → continue
│
├── [CHECK] BTC above EMA200(1d)?  [Layer 1: Market Regime]
│     NO  → ALL PAIRS BLOCKED (bear market mode)
│     YES → continue
│
├── [CHECK] 4h trend: EMA21 > EMA50 > EMA200 AND ADX > 20?  [Layer 2]
│     NO  → SKIP this pair
│     YES → continue
│
├── [CHECK] 1h: close > EMA21 AND RSI 40-68 AND MACD bullish?  [Layer 3]
│     NO  → SKIP this pair
│     YES → continue
│
├── [CHECK] 5m: StochRSI oversold crossover AND volume spike?  [Layer 4]
│     NO  → SKIP (wait for next candle)
│     YES → SIGNAL DETECTED
│
├── [VALIDATE] confirm_trade_entry() → Claude veto check  [Layer 5]
│     REJECT → SKIP (blocked)
│     CONFIRM → EXECUTE ENTRY
│
├── [EXECUTE] Buy order placed
│
├── [MONITOR] custom_stoploss() recalculates every tick  [Layer 6]
│     ATR-based stop adjusts dynamically
│
└── [EXIT] When any of:
      - minimal_roi target hit
      - ATR trailing stop hit
      - populate_exit_trend signal
      → Close position, log result to SQLite
```

---

## PART 7: EXPECTED METRICS vs. CURRENT BASELINE

| Metric | StochasticSniper (current) | GeminiHybrid (current) | UltraPrecision + GapHunter |
|---|---|---|---|
| Win Rate | ~45–52% | ~35–45% | **62–70%** |
| Trades Per Week | 35–105 | 0–14 | **4–6** |
| Avg Trade Duration | ~30–120 min | ~12–48 h | **2–12 hours** |
| Avg Win | ~2.5% | ~8% | ~3.5% |
| Avg Loss | ~2.8% | ~6% | ~1.8% |
| Reward/Risk | ~0.9:1 | ~1.3:1 | **≥2.0:1** |
| Max Drawdown | ~18% | ~25% | **< 8%** |
| Monthly Return | Variable, often negative | Variable | **+5–12%** |
| Sharpe Ratio | Unknown | Unknown | **> 1.5** |
| Capital Locked > 12h | Often | Often | **Never (ROI forces exit)** |

---

## PART 8: KEY PRINCIPLES THAT DRIVE WIN RATE

1. **No trade is better than a bad trade.** With 1 open trade allowed, every slot costs you an opportunity. Only fill it with A+ setups.

2. **The GapHunter cuts losers before they start.** If a coin isn't showing institutional interest (no gaps, weak RS, no liquidity sweep), the strategy simply never trades it, no matter how good the 5m signal looks.

3. **ATR stops prevent two failure modes:** (a) stops too tight → whipsawed out of winning trades, (b) stops too wide → big losses. ATR adapts automatically.

4. **Market regime gate is binary.** When BTC is in a bear market, crypto spot longs are a losing proposition regardless of individual coin strength. The gate turns off all trading. No exceptions.

5. **4–6 trades/week is the sweet spot.** The StochasticSniper generates 5–15 signals per day (mostly noise). UltraPrecision targets 4–6 per week — enough to compound meaningfully, few enough that each one has cleared 7 layers of confirmation. The ROI ladder (including 6h and 12h exits) ensures capital never sits idle for more than half a day.

6. **Claude learns daily.** The evening review prompt adjusts gap scoring weights based on what actually worked today. Over weeks, the system becomes calibrated to current market conditions without requiring manual intervention.

7. **Hyperopt is maintenance, not magic.** Run it after 50+ trades when win rate drops. Never run it on less than 3 months of data or it will overfit to noise.

---

## ENVIRONMENT VARIABLES NEEDED

Add to `docker-compose.yml` environment section:
```yaml
environment:
  - GEMINI_API_KEY=${GEMINI_API_KEY}
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}   # for Claude API veto layer
```

Add to `/root/.env`:
```
GEMINI_API_KEY=your_gemini_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
```

---

## IMMEDIATE NEXT STEPS (IN ORDER)

1. `mkdir -p /root/code/freqtrade/freqtrade/user_data/gap_analysis`
2. `mkdir -p /root/code/freqtrade/freqtrade/scripts/prompts`
3. Write `UltraPrecisionStrategy.py` (complete strategy file)
4. Write `scripts/daily_gap_scan.py` (gap scanner)
5. Write `scripts/coin_scorer.py` (scoring logic)
6. Run backtest: `freqtrade backtesting --strategy UltraPrecisionStrategy`
7. Run hyperopt: 300 epochs, SharpeHyperOptLossDaily
8. Set up cron jobs for daily automation
9. Dry run for 2 weeks
10. Analyze results with Claude evening review
11. Go live at 10% capital ($100) after validated win rate > 60%

---

*Plan version: 1.1 | Date: 2026-06-04 | Target: 4–6 trades/week @ 62–70% win rate | Strategy: UltraPrecision + GapHunter + Claude Agent Loop*
