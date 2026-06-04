# GapHunter + UltraPrecision — Agentic Crypto Trading Bot

A [Freqtrade](https://www.freqtrade.io/)-based spot trading system for Binance that
separates **which coin to trade** (the *GapHunter* selection engine) from **when to
enter/exit** (the *UltraPrecision* strategy), with an optional Claude agent loop for
daily review. It currently runs in **dry-run (paper) mode**.

> **Core thesis:** most of the edge is in coin *selection*, not the entry signal.
> A scanner scores every pair 0–100 each day; the strategy only trades coins that
> clear a calibrated score gate. Backtests confirm the thesis: the *same* entries
> and exits lose **−18.9%** ungated but are profitable once the gate is applied.

---

## ⚠️ Status & honest performance

This is a **research/dry-run system, not a proven money-maker.** Numbers below are
backtests on Binance spot 5m data (fees included, **slippage not modelled**), with the
GapHunter gate replayed from no-lookahead historical watchlists.

| Configuration | Trades | Win rate | Return | Profit factor |
|---|---|---|---|---|
| Ungated (entries/exits only) | 85 | 59% | **−18.9%** | 0.72 |
| Gated, **in-sample** (Jan–Jun 2026) | 9 | 89% | **+9.3%** | 9.53 |
| Gated, **out-of-sample** (Jun 2025–Jan 2026) | 8 | ~70% | **+4.6%** | 2.40 |

Key caveats, stated plainly:
- The edge **generalises in sign** (positive in two non-overlapping windows) but the
  magnitude is **much lower out-of-sample** — treat in-sample's PF ~9 as
  regime-favourable, not typical (~PF 2.4 is more realistic).
- **Frequency is low: ~0.4 trades/week** at the default gate. The plan's original
  "4–6 trades/week at 62–70% win" is **not** simultaneously achievable with this
  scorer — selectivity is what pays.
- Samples are **small** (8–19 trades/window): directional evidence, not precision.
- A **forward dry-run** is running to test live fills/slippage/timing before any
  capital is risked. **Do not trade real money on this without your own validation.**

See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the original design rationale.

---

## How it works

```
            ┌──────────────── DAILY (06:00 UTC, host cron) ────────────────┐
            │  download fresh OHLCV  →  GapHunter scan  →  daily_scores.json │
            │  (5m/1h/4h/1d)            (score all pairs    (watchlist of    │
            │                            0–100, gate ≥45)    coins ≥ gate)   │
            └───────────────────────────────┬──────────────────────────────┘
                                            │
                           ┌────────────────▼─────────────────┐
                           │      UltraPrecisionStrategy        │
                           │  L1 Market regime gate (BTC 1d)    │
                           │  L2 4h trend (EMA stack+ADX+RSI)    │
                           │  L3 1h trend (EMA+RSI band+MACD)    │
                           │  L4 5m trigger (StochRSI+vol+BB)    │
                           │  L5 GapHunter veto (watchlist gate) │
                           │  L6 ATR dynamic stop (2.0×ATR)      │
                           │  L7 Exit: 1h trend-break + ROI ladder│
                           └────────────────┬─────────────────┘
                                            │ dry-run fills
                                  tradesv3.sqlite + Telegram
```

### GapHunter — the coin scorer (`scripts/coin_scorer.py`)
Each pair gets a 0–100 score from six "gap" detectors:

| # | Signal | Max pts |
|---|---|---|
| 1 | Fair Value Gap (unmitigated bullish imbalance) | 25 |
| 2 | Volume Profile void (price in a low-volume node) | 20 |
| 3 | Relative Strength vs BTC (1d/3d/7d) | 20 |
| 4 | Fibonacci retracement (golden pocket) | 15 |
| 5 | Liquidity sweep (stop-hunt + reclaim) | 15 |
| 6 | Time-of-day volume window | 5 |

In practice the daily *best* score has median ~43 and max ~62, so the plan's
`min_score = 60` is effectively unreachable. The **validated gate is 45**
(`gap_score_threshold` in the strategy), which was the most robust out-of-sample.

### UltraPrecision — the executor (`user_data/strategies/UltraPrecisionStrategy.py`)
A 5m strategy with 1h/4h/1d informative timeframes. `confirm_trade_entry` is the
GapHunter gate: in **live/dry-run** it reads today's `daily_scores.json`; in
**backtest/hyperopt** it replays `historical_watchlists.json` (so backtests actually
exercise coin selection). An empty watchlist means **stay flat** (fail-closed).

Notable tuning (from diagnosing a −30% original version — see git history):
- **Exit (L7)** reduced to the 1h trend-break only; the old 5m momentum/volume-dry-up
  exits force-closed ~90% of trades on noise.
- **ATR stop** widened to **2.0×ATR(1h)** — dip-buy entries were whipsawed at 1.5×.
- **Break-even ratchet** is implemented but **off by default** (`use_breakeven`),
  because it backtested net-negative.

---

## Repository layout

```
.
├── README.md                     ← this file
├── IMPLEMENTATION_PLAN.md        ← full design rationale
├── prompts.md
└── freqtrade/
    ├── docker-compose.yml        ← runs the bot (dry-run) in a container
    ├── scripts/
    │   ├── coin_scorer.py            ← 6-gap scoring engine
    │   ├── daily_gap_scan.py         ← live daily scan → daily_scores.json
    │   ├── backtest_watchlists.py    ← historical per-day watchlists (no lookahead)
    │   ├── config_updater.py         ← (agentic loop) rewrite whitelist from a decision
    │   ├── performance_extractor.py  ← read trade DB → stats JSON
    │   ├── hyperopt_trigger.py       ← auto-hyperopt when win rate drops
    │   ├── apply_weights.py          ← apply adaptive score weights
    │   ├── run_daily_cycle.sh        ← full agentic morning cycle (Claude optional)
    │   ├── run_evening_cycle.sh      ← evening learning/review cycle
    │   ├── run_dryrun_refresh.sh     ← minimal daily refresh for the dry-run
    │   ├── dryrun_monitor.sh         ← read-only status probe + Telegram alerts
    │   └── prompts/                  ← Claude morning/evening review prompts
    └── user_data/
        ├── config.json           ← NOT in git (secrets); see config template below
        ├── backtest_config.json  ← StaticPairList overlay for reproducible backtests
        ├── strategies/UltraPrecisionStrategy.py
        ├── data/                 ← OHLCV (gitignored)
        └── gap_analysis/         ← daily_scores.json, historical_watchlists.json (gitignored)
```

**Gitignored (never committed):** `config.json` (Telegram token + API password),
`*.bak`, `archive/`, `tradesv3.sqlite*`, `user_data/data/`, `user_data/gap_analysis/`,
`user_data/logs/`. See [`.gitignore`](.gitignore).

---

## Setup

### Prerequisites
- Docker + Docker Compose, Python 3 on the host, a Binance account (spot; no API keys
  needed for dry-run), and optionally a Telegram bot for alerts.

### 1. Configuration
`user_data/config.json` is gitignored because it holds secrets. Create it from the
Freqtrade schema with at least:

```jsonc
{
  "max_open_trades": 1,
  "stake_currency": "USDT",
  "stake_amount": "unlimited",
  "tradable_balance_ratio": 0.99,
  "dry_run": true,
  "dry_run_wallet": 1000,
  "trading_mode": "spot",
  "exchange": { "name": "binance", "pair_whitelist": [ "BTC/USDT", "ETH/USDT", "..." ] },
  "pairlists": [ { "method": "StaticPairList" },
                 { "method": "SpreadFilter", "max_spread_ratio": 0.005 } ],
  "telegram": { "enabled": true, "token": "YOUR_BOT_TOKEN", "chat_id": "YOUR_CHAT_ID" },
  "api_server": { "enabled": true, "listen_ip_address": "127.0.0.1", "listen_port": 8080,
                  "username": "...", "password": "...", "jwt_secret_key": "...", "ws_token": "..." }
}
```

The forward dry-run uses a **StaticPairList of 25 validated pairs** + the GapHunter
gate. (The full agentic `run_daily_cycle.sh` instead rewrites the whitelist daily.)

### 2. Download market data
```bash
cd freqtrade
docker exec freqtrade freqtrade download-data \
  --pairs BTC/USDT ETH/USDT ...   --timeframes 5m 1h 4h 1d --days 365 --prepend
```
The scanner/strategy work off 5m and resample 1h/4h internally; download 1d too for
the BTC regime gate.

### 3. Run the bot (dry-run)
```bash
cd freqtrade
docker compose up -d --force-recreate freqtrade
docker logs -f freqtrade        # expect: "Dry run is enabled", strategy resolved, heartbeat
```

---

## Usage

### Generate today's watchlist (live)
```bash
docker exec freqtrade python3 /freqtrade/scripts/daily_gap_scan.py \
  --data-dir /freqtrade/user_data/data/binance \
  --output   /freqtrade/user_data/gap_analysis/daily_scores.json \
  --min-score 45
```
The strategy reloads it automatically on file change (no restart needed).

### Backtest (with the GapHunter gate)
```bash
# 1) build no-lookahead historical watchlists for the range
docker exec freqtrade python3 /tmp/gh_scripts/backtest_watchlists.py \
  --data-dir /freqtrade/user_data/data/binance \
  --output   /freqtrade/user_data/gap_analysis/historical_watchlists.json \
  --start 2025-06-20 --end 2026-06-04

# 2) backtest (StaticPairList overlay so backtesting works)
docker exec freqtrade freqtrade backtesting \
  --strategy UltraPrecisionStrategy \
  --config /freqtrade/user_data/config.json \
  --config /freqtrade/user_data/backtest_config.json \
  --timeframe 5m --timerange 20260117- --cache none
```
> Note: a full-year, 25-pair 5m backtest can OOM in a small container — run in windows.

### Hyperopt (optional)
```bash
docker exec freqtrade freqtrade hyperopt \
  --strategy UltraPrecisionStrategy --hyperopt-loss SharpeHyperOptLossDaily \
  --spaces buy sell stoploss --epochs 200 \
  --config /freqtrade/user_data/config.json --config /freqtrade/user_data/backtest_config.json
```

---

## Automated daily cycle & monitoring (host cron)

```cron
# refresh the watchlist each morning
0 6 * * *   .../scripts/run_dryrun_refresh.sh   >> .../logs/dryrun_refresh.log 2>&1
# watch for changes every 15m → Telegram alert on any change (bot down, errors, trades)
*/15 * * * * .../scripts/dryrun_monitor.sh       >> .../logs/dryrun_monitor.log 2>&1
# daily heartbeat summary to Telegram
0 7 * * *   .../scripts/dryrun_monitor.sh force  >> .../logs/dryrun_monitor.log 2>&1
```

`dryrun_monitor.sh` is read-only: it reports deltas (new/closed trades + P/L, opened
positions, watchlist shifts, bot-down, real ERROR/Traceback) and sends Telegram alerts
using the token/chat_id from `config.json`. Freqtrade itself also Telegrams every
buy/sell. On-demand status anytime:

```bash
bash freqtrade/scripts/dryrun_monitor.sh
```

The bot (`restart: always`) and cron run as long as the **host/VM is powered on** —
independent of any terminal or agent session.

---

## Tuning

| Knob | Where | Default | Notes |
|---|---|---|---|
| Coin gate | `gap_score_threshold` | 45 | 40 = more trades/lower PF; 50 = fewer/higher PF |
| ATR stop | `atr_stop_multiplier` | 2.0 | wider suits 5m dip-buys; 1.0 whipsaws |
| Break-even | `use_breakeven` | False | backtested net-negative; left off |
| ROI ladder | `minimal_roi` | 6%→0.5% | early rungs bank quick movers |
| Scan threshold | `--min-score` (scan) | 45 | keep in sync with `gap_score_threshold` |

---

## Disclaimer

Educational/research software. Crypto trading carries substantial risk of loss. The
backtests here are small-sample, single-exchange, and do not model slippage. Nothing
here is financial advice. Run in dry-run and validate independently before risking
any capital. Use at your own risk.
