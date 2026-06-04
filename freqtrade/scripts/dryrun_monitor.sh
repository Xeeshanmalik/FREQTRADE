#!/bin/bash
# dryrun_monitor.sh — one-shot status probe for the UltraPrecision forward
# dry-run. Prints a compact status line plus any CHANGES since the last run
# (new closed trades, opened trades, watchlist shifts, bot-down / errors).
# State is kept in user_data/logs/monitor_state.json so repeated calls can
# report deltas. Exit/print is cheap and read-only — safe to call on a timer.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="freqtrade"
STATE="$REPO/user_data/logs/monitor_state.json"
SCORES="$REPO/user_data/gap_analysis/daily_scores.json"

UP=$(docker ps --filter name="$CONTAINER" --format '{{.Status}}' 2>/dev/null)
# Match the LEVEL field ("- ERROR -"/"- CRITICAL -") or a traceback, not the bare
# word — the API logger is literally named "uvicorn.error" and logs at INFO.
ERRS=$(docker logs --since 65m "$CONTAINER" 2>&1 | grep -icE " - (ERROR|CRITICAL) - |Traceback \(most recent")

python3 - "$STATE" "$SCORES" "$UP" "$ERRS" <<'PY'
import json, sqlite3, sys, os, subprocess, datetime
state_path, scores_path, up, errs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

# --- trades from the container DB ---
tot=op=closed=0; pnl=0.0; recent=[]
try:
    out = subprocess.run(["docker","exec","freqtrade","python3","-c",
        "import sqlite3,json;c=sqlite3.connect('/freqtrade/user_data/tradesv3.sqlite');"
        "rows=[dict(id=r[0],pair=r[1],is_open=r[2],pnl=r[3],reason=r[4],open=r[5],close=r[6]) "
        "for r in c.execute('select id,pair,is_open,close_profit,exit_reason,open_date,close_date from trades order by id')];"
        "print(json.dumps(rows))"], capture_output=True, text=True, timeout=30)
    rows = json.loads(out.stdout.strip() or "[]")
    tot=len(rows); op=sum(1 for r in rows if r["is_open"]); closed=tot-op
    pnl=sum((r["pnl"] or 0) for r in rows if not r["is_open"])*100
    recent=rows[-3:]
except Exception as e:
    print(f"[{now}] WARN could not read trades DB: {e}")

# --- watchlist ---
wl=[]
try:
    d=json.load(open(scores_path)); wl=[w["pair"] for w in d["watchlist"]]
except Exception: pass

# --- prior state ---
prev={}
if os.path.exists(state_path):
    try: prev=json.load(open(state_path))
    except Exception: pass

changes=[]
if not up: changes.append("BOT DOWN — container not running")
if errs: changes.append(f"{errs} error/traceback line(s) in last 65m")
if prev.get("closed",0) != closed and "closed" in prev:
    new=closed-prev.get("closed",0)
    changes.append(f"{new:+d} closed trade(s) (now {closed}, cum P/L {pnl:+.2f}%)")
if prev.get("open",0) != op and "open" in prev:
    changes.append(f"open trades {prev.get('open',0)} -> {op}")
if prev.get("watchlist") is not None and prev["watchlist"] != wl:
    changes.append(f"watchlist {prev.get('watchlist')} -> {wl}")

status = "UP" if up else "DOWN"
print(f"[{now}] {status} | trades tot={tot} open={op} closed={closed} P/L={pnl:+.2f}% | watchlist={wl} | errs65m={errs}")
if changes:
    print("CHANGES:")
    for c in changes: print("  -", c)
    if recent:
        print("recent trades:", json.dumps(recent))
else:
    print("no change since last check")

json.dump({"closed":closed,"open":op,"tot":tot,"pnl":pnl,"watchlist":wl,"ts":now}, open(state_path,"w"), indent=2)
PY
