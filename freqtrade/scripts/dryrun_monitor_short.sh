#!/bin/bash
# dryrun_monitor_short.sh — read-only status probe for the ShortPrecision forward
# dry-run (container freqtrade-short, DB tradesv3_short.sqlite). Sibling of
# dryrun_monitor.sh; same delta-tracking + Telegram-on-change design.
#
# Telegram: reuses the SAME token/chat_id from config.json (send-only via the Bot
# API is stateless and does NOT conflict with either freqtrade instance's own
# Telegram long-poll — only getUpdates polling conflicts). Messages are tagged
# "🔻 SHORT" so they're distinguishable from the long bot's alerts.
#
# Also reports the BTC regime (the short bot only trades while BTC < EMA200(1d)),
# so a quiet bot in a bull regime reads as expected rather than broken.
#
# Usage: dryrun_monitor_short.sh [force]   # "force" => always send a summary
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="freqtrade-short"
STATE="$REPO/user_data/logs/monitor_state_short.json"
CONFIG="$REPO/user_data/config.json"
FORCE="${1:-}"

UP=$(docker ps --filter name="$CONTAINER" --format '{{.Status}}' 2>/dev/null)
ERRS=$(docker logs --since 65m "$CONTAINER" 2>&1 | grep -icE " - (ERROR|CRITICAL) - |Traceback \(most recent")

python3 - "$STATE" "$UP" "$ERRS" "$CONFIG" "$FORCE" "$CONTAINER" <<'PY'
import json, sys, os, subprocess, datetime, urllib.request, urllib.parse
state_path, up, errs, config_path, force, container = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6])
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# --- trades from the short container DB ---
tot=op=closed=0; pnl=0.0; recent=[]; open_pairs=[]
try:
    out = subprocess.run(["docker","exec",container,"python3","-c",
        "import sqlite3,json;c=sqlite3.connect('/freqtrade/user_data/tradesv3_short.sqlite');"
        "rows=[dict(id=r[0],pair=r[1],is_open=r[2],pnl=r[3],reason=r[4]) "
        "for r in c.execute('select id,pair,is_open,close_profit,exit_reason from trades order by id')];"
        "print(json.dumps(rows))"], capture_output=True, text=True, timeout=30)
    rows = json.loads(out.stdout.strip() or "[]")
    tot=len(rows); op=sum(1 for r in rows if r["is_open"]); closed=tot-op
    pnl=sum((r["pnl"] or 0) for r in rows if not r["is_open"])*100
    open_pairs=[r["pair"] for r in rows if r["is_open"]]
    recent=rows[-3:]
except Exception as e:
    print(f"[{now}] WARN could not read short trades DB: {e}")

# --- BTC regime (the short bot only trades when bear) ---
regime="unknown"
try:
    out = subprocess.run(["docker","exec",container,"python3","-c",
        "import pandas as pd,talib.abstract as ta;"
        "df=pd.read_feather('/freqtrade/user_data/data/binance/futures/BTC_USDT_USDT-1d-futures.feather');"
        "df['e']=ta.EMA(df,timeperiod=200);r=df.dropna(subset=['e']).iloc[-1];"
        "print('BEAR' if r.close<r.e else 'BULL')"], capture_output=True, text=True, timeout=30)
    regime = (out.stdout.strip() or "unknown")
except Exception: pass

# --- prior state ---
prev={}
if os.path.exists(state_path):
    try: prev=json.load(open(state_path))
    except Exception: pass

changes=[]
if not up: changes.append("BOT DOWN — container not running")
if errs: changes.append(f"{errs} error/traceback line(s) in last 65m")
if "closed" in prev and prev.get("closed",0) != closed:
    changes.append(f"{closed-prev.get('closed',0):+d} closed trade(s) (now {closed}, cum P/L {pnl:+.2f}%)")
if "open" in prev and prev.get("open",0) != op:
    changes.append(f"open trades {prev.get('open',0)} -> {op} {open_pairs}")

status = "UP" if up else "DOWN"
headline = (f"[{now}] {status} | regime={regime} | trades tot={tot} open={op} "
            f"closed={closed} P/L={pnl:+.2f}% | open={open_pairs} | errs65m={errs}")
print(headline)
if changes:
    print("CHANGES:")
    for c in changes: print("  -", c)
    if recent: print("recent trades:", json.dumps(recent))
else:
    print("no change since last check")

def telegram(msg):
    try:
        t=json.load(open(config_path)).get("telegram",{})
        tok, chat = t.get("token"), str(t.get("chat_id",""))
        if not (t.get("enabled") and tok and chat): return
        data=urllib.parse.urlencode({"chat_id":chat,"text":msg,"disable_notification":False}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
        print("(telegram sent)")
    except Exception as e:
        print(f"(telegram failed: {e})")

# Trade open/close alerts now come from the short bot's OWN native Telegram
# (@Gaphuntershortbot). This host watchdog only pushes when the bot is DOWN or
# erroring — which native Telegram cannot report (a dead bot can't message).
critical = [c for c in changes if "DOWN" in c or "error" in c]
if critical:
    telegram("⚠️ SHORT bot watchdog\n" + headline + "\n" + "\n".join("• "+c for c in critical))
elif force == "force":
    telegram("🔻 SHORT watchdog heartbeat\n" + headline)

json.dump({"closed":closed,"open":op,"tot":tot,"pnl":pnl,"regime":regime,"ts":now}, open(state_path,"w"), indent=2)
PY
