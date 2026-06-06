#!/bin/bash
# weekly_profit_report.sh — push a weekly dry-run P&L digest to Telegram.
#
# Reads closed trades from the container DB and the wallet size from config.json,
# computes last-7d and all-time stats, and sends one Telegram message (token/
# chat_id read from config.json at runtime, never hardcoded — same mechanism as
# dryrun_monitor.sh). Designed for host cron so it survives any agent session.
#
# Usage: weekly_profit_report.sh        # always sends the digest
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="freqtrade"
CONFIG="$REPO/user_data/config.json"

UP=$(docker ps --filter name="$CONTAINER" --format '{{.Status}}' 2>/dev/null)

python3 - "$CONFIG" "$UP" <<'PY'
import json, sys, subprocess, datetime, urllib.request, urllib.parse

config_path, up = sys.argv[1], sys.argv[2]
now = datetime.datetime.now(datetime.timezone.utc)
ts = now.strftime("%Y-%m-%d %H:%M UTC")
week_ago = now - datetime.timedelta(days=7)

cfg = {}
try:
    cfg = json.load(open(config_path))
except Exception:
    pass
wallet = float(cfg.get("dry_run_wallet", 1000) or 1000)
gate = "?"  # informational only

# --- pull all trades from the container DB ---
rows = []
try:
    out = subprocess.run(["docker", "exec", "freqtrade", "python3", "-c",
        "import sqlite3,json;c=sqlite3.connect('/freqtrade/user_data/tradesv3.sqlite');"
        "print(json.dumps([dict(id=r[0],pair=r[1],is_open=r[2],profit=r[3],profit_abs=r[4],"
        "close_date=r[5]) for r in c.execute("
        "'select id,pair,is_open,close_profit,close_profit_abs,close_date from trades')]))"],
        capture_output=True, text=True, timeout=30)
    rows = json.loads(out.stdout.strip() or "[]")
except Exception as e:
    print(f"[{ts}] WARN could not read trades DB: {e}")

def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s[:26], fmt).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
    return None

closed = [r for r in rows if not r["is_open"]]
open_n = sum(1 for r in rows if r["is_open"])

def stats(trades):
    n = len(trades)
    if not n:
        return n, 0, 0.0, 0.0
    wins = sum(1 for t in trades if (t["profit_abs"] or 0) > 0)
    pnl_abs = sum((t["profit_abs"] or 0) for t in trades)
    pnl_pct = sum((t["profit"] or 0) for t in trades) * 100
    return n, round(100 * wins / n), round(pnl_abs, 2), round(pnl_pct, 2)

c7 = [t for t in closed if (parse_dt(t["close_date"]) or now) >= week_ago]
n7, win7, abs7, pct7 = stats(c7)
nA, winA, absA, pctA = stats(closed)
balance = round(wallet + absA, 2)
ret_pct = round(100 * absA / wallet, 2) if wallet else 0.0

status = "🟢 running" if up else "🔴 DOWN"
lines = [
    f"📊 Weekly dry-run report — {ts}",
    f"Bot: {status}",
    "",
    f"Last 7 days: {n7} closed trade(s)"
    + (f", {win7}% win, P/L {abs7:+.2f} USDT ({pct7:+.2f}%)" if n7 else " — no trades this week"),
    f"All-time:   {nA} closed, {open_n} open"
    + (f", {winA}% win" if nA else ""),
    f"Cumulative P/L: {absA:+.2f} USDT ({ret_pct:+.2f}% of {wallet:.0f})",
    f"Sim. balance: {balance:.2f} USDT",
]
if nA == 0:
    lines.append("")
    lines.append("No trades yet — the strategy is selective (gate 40, ~0.6 trades/wk); it only fires on A+ setups. Still dry-run, no real money.")
msg = "\n".join(lines)
print(msg)

def telegram(text):
    try:
        t = cfg.get("telegram", {})
        tok, chat = t.get("token"), str(t.get("chat_id", ""))
        if not (t.get("enabled") and tok and chat):
            print("(telegram not configured)"); return
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
        print("(telegram sent)")
    except Exception as e:
        print(f"(telegram failed: {e})")

telegram(msg)
PY
