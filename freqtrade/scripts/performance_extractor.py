#!/usr/bin/env python3
"""
performance_extractor.py — summarise recent trading performance from the DB.

Reads freqtrade's tradesv3.sqlite and emits a JSON summary (win rate, average
profit, per-trade detail) consumed by the Claude morning/evening reviews and the
hyperopt trigger. Read-only: it never modifies the database.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone


def extract_performance(db_path: str, days: int = 7) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # freqtrade stores the closed-trade return as close_profit (ratio) and
        # close_profit_abs (absolute), not profit_ratio/profit_abs.
        rows = conn.execute(
            """
            SELECT pair, close_profit, close_profit_abs, open_date, close_date,
                   open_rate, close_rate, exit_reason
            FROM trades
            WHERE is_open = 0 AND close_date >= ?
            ORDER BY close_date DESC
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"period_days": days, "total_trades": 0, "win_rate": 0, "trades": []}

    trades = [
        {
            "pair": r["pair"],
            "profit_pct": round((r["close_profit"] or 0) * 100, 3),
            "profit_usdt": round(r["close_profit_abs"] or 0, 4),
            "open_date": r["open_date"],
            "close_date": r["close_date"],
            "open_rate": r["open_rate"],
            "close_rate": r["close_rate"],
            "exit_reason": r["exit_reason"],
        }
        for r in rows
    ]

    total = len(trades)
    wins = [t for t in trades if t["profit_pct"] > 0]
    # Per exit-reason breakdown helps the evening review spot which exits leak.
    by_reason: dict = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"] or "unknown", {"n": 0, "wins": 0})
        by_reason[t["exit_reason"] or "unknown"]["n"] += 1
        if t["profit_pct"] > 0:
            by_reason[t["exit_reason"] or "unknown"]["wins"] += 1

    return {
        "period_days": days,
        "total_trades": total,
        "wins": len(wins),
        "losses": total - len(wins),
        "win_rate": round(len(wins) / total, 3),
        "avg_profit_pct": round(sum(t["profit_pct"] for t in trades) / total, 3),
        "total_profit_usdt": round(sum(t["profit_usdt"] for t in trades), 4),
        "by_exit_reason": by_reason,
        "trades": trades,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarise freqtrade performance")
    parser.add_argument("--db", required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    stats = extract_performance(args.db, args.days)
    with open(args.output, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"Extracted {stats['total_trades']} trades. Win rate: {stats['win_rate']}")
