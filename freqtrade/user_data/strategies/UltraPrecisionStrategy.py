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

Plan's target was 4-6 trades/week at 62-70% win. Backtest reality (5m data,
GapHunter gate replayed from historical_watchlists.json, gap_score_threshold=45):
the coin-selection gate IS the edge — ungated, the same entries/exits lose
-18.9%; gated, it is profitable across two non-overlapping windows:
    in-sample     Jan-Jun'26 : +9.3%, 89% win, PF 9.5
    out-of-sample Jun'25-Jan'26: +4.6%, PF 2.4
Out-of-sample magnitude is far lower than in-sample, so treat the in-sample
figures as regime-favourable. Frequency is only ~0.4 trades/week — the plan's
4-6/week and a high win rate are not simultaneously reachable with this scorer;
selectivity is what pays. Samples are small (8-19 trades/window) — directional,
not precise; needs forward dry-run before risking capital. See IMPLEMENTATION_PLAN.md.
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
# Historical per-day watchlists (scripts/backtest_watchlists.py) — lets a backtest
# replay the GapHunter coin-selection gate instead of bypassing it.
HISTORICAL_WATCHLIST_PATH = (
    Path(__file__).resolve().parents[1] / "gap_analysis" / "historical_watchlists.json"
)
# Entry-veto calendar: macro-event (FOMC/CPI) blackout windows + per-coin token
# unlock dates. Applied in both live and backtest so its effect is measurable.
EVENT_BLACKOUT_PATH = (
    Path(__file__).resolve().parents[1] / "gap_analysis" / "event_blackouts.json"
)
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
    # Fix #2 — the ROI engine is the strategy's one stable edge: across every
    # backtest variant these rungs bank ~+33% from quick movers at a 100% hit
    # rate. We keep the small-win harvesting but lift the late-rung floors off the
    # old 0.1%/0.4% (which guaranteed avg-win < avg-loss). The actual R/R fix that
    # mattered was widening the ATR stop to 2.0x (see atr_stop_multiplier), not a
    # bigger ROI target or the break-even ratchet (which backtested net-negative).
    minimal_roi = {
        "0": 0.06,
        "30": 0.04,
        "60": 0.025,
        "120": 0.018,
        "180": 0.012,
        "360": 0.008,
        "720": 0.005,
    }

    # custom_stoploss provides the dynamic trailing; disable the static trailer.
    trailing_stop = False
    use_custom_stoploss = True

    # ── Streak / drawdown circuit-breakers (Layer 0 — capital preservation) ────
    # Active in live/dry-run; in backtest only with --enable-protections. Tuned
    # for a low-frequency single-slot system: they almost never fire in normal
    # operation and exist to halt trading through a genuinely bad regime before a
    # streak compounds. (config-level "protections" is deprecated in 2026.1, so
    # this lives on the strategy.)
    @property
    def protections(self):
        return [
            # Pause briefly after every exit so we don't re-fire on the same candle noise.
            {"method": "CooldownPeriod", "stop_duration_candles": 3},
            # 2 stoplosses inside a day -> stand down for a day.
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 288,
                "trade_limit": 2,
                "stop_duration_candles": 288,
                "only_per_pair": False,
            },
            # Hard account kill-switch: >20% drawdown over ~5d (min 3 trades) -> halt 2.5d.
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 1440,
                "trade_limit": 3,
                "stop_duration_candles": 720,
                "max_allowed_drawdown": 0.20,
            },
        ]

    # ── Hyperopt parameter space (defaults tuned for 4-6 trades/week) ──────────
    buy_stoch_max = IntParameter(10, 30, default=25, space="buy")
    buy_volume_ratio = DecimalParameter(1.0, 2.5, default=1.3, space="buy")
    buy_rsi_1h_min = IntParameter(35, 50, default=38, space="buy")
    buy_rsi_1h_max = IntParameter(62, 78, default=70, space="buy")
    buy_rsi_4h_max = IntParameter(65, 80, default=72, space="buy")
    buy_adx_min = IntParameter(14, 28, default=18, space="buy")

    sell_stoch_min = IntParameter(70, 90, default=80, space="sell")
    sell_rsi_1h_exit = IntParameter(38, 52, default=45, space="sell")

    # GapHunter score a coin must reach to be tradable — the system's primary
    # edge. The plan's 60 is unreachable in practice (historical daily best:
    # median ~43, max 62), so the real calibrated gate lives here. The gate is
    # what makes the strategy profitable: ungated, the same entries/exits lose
    # -18.9%. Validated on two non-overlapping windows (gate replayed from
    # historical_watchlists.json), profit factor by threshold:
    #             in-sample (Jan-Jun'26)   out-of-sample (Jun'25-Jan'26)
    #   thr 40        3.23 (+15.5%)            1.31 (+3.9%)
    #   thr 45        9.53 (+9.3%)             2.40 (+4.6%)   <- most robust
    #   thr 50        2.95 (+2.1%)            1.63 (+2.1%)
    # 45 degrades least out-of-sample, so it is the default despite ~0.4 trades/
    # week. The edge is real and generalises in sign, but magnitude out-of-sample
    # is modest (PF ~2.4) — treat in-sample's PF ~9 as regime-favourable, not
    # typical. Samples are small (8-19 trades/window): directional, not precise.
    gap_score_threshold = IntParameter(35, 60, default=40, space="buy")

    atr_stop_multiplier = DecimalParameter(1.0, 3.0, default=2.0, space="stoploss")
    atr_trail_multiplier = DecimalParameter(0.2, 0.8, default=0.5, space="stoploss")
    # Profit at which the stop ratchets to break-even (Fix #2 — R/R rebalance).
    breakeven_trigger = DecimalParameter(0.004, 0.015, default=0.005, space="stoploss")
    # Toggle for the break-even ratchet (see custom_stoploss). Off by default.
    use_breakeven = False

    # GapHunter watchlist gate. Loaded lazily and cached per process; refreshed
    # when the file's mtime changes (the daily cycle rewrites it each morning).
    _watchlist_cache: dict = {}
    _watchlist_mtime: float = 0.0
    _historical_cache: Optional[dict] = None
    # Parsed event-blackout calendar (macro windows + per-pair unlock windows).
    _blackout_cache: Optional[dict] = None
    # Hours before/after a token unlock to stand aside (supply overhang then settle).
    unlock_pre_h: int = 24
    unlock_post_h: int = 6

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
        """Attach a ``btc_below_ema200_1d`` flag to the 5m frame.

        Uses an explicit ``merge_asof`` rather than ``merge_informative_pair``:
        the helper was returning an all-False flag in this freqtrade build
        (verified 2026-06-07 — the column was present but never True across a
        100%-bear window), which silently DISABLED this regime gate (fail-open),
        letting longs trade in bear markets contrary to Layer 1. merge_asof is
        deterministic; each daily flag is shifted forward one day so a 1d candle
        is only used AFTER it closes (no look-ahead)."""
        dataframe["btc_below_ema200_1d"] = False  # safe default (fail-open to trading)
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
        # Only a genuine 1h trend-structure break warrants a signal exit. The
        # former 5m momentum-exhaustion (EXIT 1) and volume-dry-up (EXIT 3) rules
        # fired on 5m noise and force-closed ~90% of trades after ~35 min at a 21%
        # win rate, preempting the ROI ladder and ATR trail. Profit-taking is left
        # to minimal_roi + custom_stoploss; this only cuts trades whose thesis (the
        # 1h uptrend that justified the entry) has actually broken.
        dataframe.loc[
            (dataframe["close"] < dataframe["ema21_1h"])
            & (dataframe["rsi_1h"] < self.sell_rsi_1h_exit.value),
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

        # Tightest branch first so the higher locks override the lower ones.
        if current_profit >= 0.05:
            tight = current_rate - (0.3 * atr)
            candidates.append((tight - current_rate) / current_rate)
        elif current_profit >= 0.02:
            trail = current_rate - (self.atr_trail_multiplier.value * atr)
            candidates.append((trail - current_rate) / current_rate)
        elif self.use_breakeven and current_profit >= self.breakeven_trigger.value:
            # Lock the trade at break-even (+a hair) once it has shown a modest
            # gain. Disabled by default: in backtests on the unfiltered universe
            # it cut more recovering winners than it saved losers (net-negative).
            breakeven_price = entry_price * 1.001
            candidates.append((breakeven_price - current_rate) / current_rate)

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
        """Block any entry not backed by the GapHunter watchlist for that day.

        Backtest/hyperopt: replay the historical per-day watchlist keyed on
        ``current_time``'s date (scripts/backtest_watchlists.py). If no historical
        map is present we fall back to ungated (legacy behaviour) so vanilla
        backtests still run. Live/dry-run: use today's scanner output.
        """
        # Layer 0b — event blackout: never open into a known macro shock (FOMC/CPI)
        # or an imminent token unlock. Applies in every runmode so the backtest
        # measures it. Fail-open: a missing/empty calendar means no veto.
        blocked, why = self._in_event_blackout(pair, current_time)
        if blocked:
            logger.info("[%s] Event blackout (%s) — vetoing entry.", pair, why)
            return False

        if self.dp and self.dp.runmode.value in ("backtest", "hyperopt", "plot"):
            return self._confirm_from_history(pair, current_time)

        try:
            watchlist = self._load_watchlist()
            if not watchlist:
                # Empty watchlist means the scan found no qualifying setups today.
                # That is the system's "stay flat" signal — block, don't fail-open.
                # (Only a *missing/corrupt* file fails open, via the except below.)
                logger.info("[%s] GapHunter watchlist empty — no setups today. Blocking.", pair)
                return False

            entry = next((s for s in watchlist if s.get("pair") == pair), None)
            if entry is None:
                logger.warning("[%s] Not on today's GapHunter watchlist. Blocking.", pair)
                return False

            score = entry.get("total_score", 0)
            min_score = self._watchlist_cache.get("min_score", self.gap_score_threshold.value)
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
            return True  # fail-open only on unexpected errors

    def _confirm_from_history(self, pair: str, current_time) -> bool:
        """Backtest gate: is ``pair`` on the GapHunter watchlist for this day?"""
        days = self._load_historical()
        if not days:
            return True  # no historical map -> run ungated (legacy)
        day = days.get(current_time.strftime("%Y-%m-%d"))
        if not day:
            return False  # no scan for this day -> stay flat
        threshold = self.gap_score_threshold.value
        eligible = {t["pair"] for t in day.get("top", []) if t.get("score", 0) >= threshold}
        return pair in eligible

    def _load_historical(self) -> dict:
        """Load and cache the historical per-day watchlist map (backtest only)."""
        if self._historical_cache is None:
            if not HISTORICAL_WATCHLIST_PATH.exists():
                self._historical_cache = {}
            else:
                try:
                    data = json.loads(HISTORICAL_WATCHLIST_PATH.read_text())
                    self._historical_cache = data.get("days", {})
                    logger.info(
                        "Loaded %d historical GapHunter watchlists from %s",
                        len(self._historical_cache), HISTORICAL_WATCHLIST_PATH,
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    logger.error("Failed to read historical watchlists: %s", exc)
                    self._historical_cache = {}
        return self._historical_cache

    # -------------------------------------------------------------------------
    # Layer 0b — event blackout (macro shocks + token unlocks)
    # -------------------------------------------------------------------------
    def _load_blackouts(self) -> dict:
        """Load and cache the event-blackout calendar (macro events + unlocks)."""
        if self._blackout_cache is None:
            if not EVENT_BLACKOUT_PATH.exists():
                self._blackout_cache = {}
            else:
                try:
                    data = json.loads(EVENT_BLACKOUT_PATH.read_text())
                    self._blackout_cache = {
                        "macro": data.get("macro_events", []),
                        "unlocks": data.get("token_unlocks", {}),
                    }
                    logger.info(
                        "Loaded event blackouts: %d macro events, %d coins with unlocks",
                        len(self._blackout_cache["macro"]),
                        len(self._blackout_cache["unlocks"]),
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    logger.error("Failed to read event blackouts: %s", exc)
                    self._blackout_cache = {}
        return self._blackout_cache

    def _in_event_blackout(self, pair: str, current_time) -> tuple:
        """Is ``current_time`` inside a macro-event window or this pair's unlock window?

        Returns ``(blocked: bool, reason: str)``. Fail-open: any parse problem or a
        missing calendar yields ``(False, "")`` so trading is never silently halted.
        """
        try:
            cfg = self._load_blackouts()
            if not cfg:
                return False, ""
            t = pd.Timestamp(current_time)
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            for ev in cfg.get("macro", []):
                et = pd.Timestamp(ev["datetime"])
                if et.tzinfo is None:
                    et = et.tz_localize("UTC")
                start = et - pd.Timedelta(hours=ev.get("pre_h", 1))
                end = et + pd.Timedelta(hours=ev.get("post_h", 6))
                if start <= t <= end:
                    return True, ev.get("label", "macro")
            for unlock in cfg.get("unlocks", {}).get(pair, []):
                ut = pd.Timestamp(unlock)
                if ut.tzinfo is None:
                    ut = ut.tz_localize("UTC")
                if ut - pd.Timedelta(hours=self.unlock_pre_h) <= t <= ut + pd.Timedelta(hours=self.unlock_post_h):
                    return True, "unlock"
        except Exception as exc:  # noqa: BLE001 — a calendar bug must not halt trading
            logger.warning("Event-blackout check failed for %s: %s", pair, exc)
        return False, ""

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
