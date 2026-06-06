#!/usr/bin/env python3
"""
update_event_blackouts.py — maintain user_data/gap_analysis/event_blackouts.json,
the entry-veto calendar consumed by UltraPrecisionStrategy._in_event_blackout().

Two parts:

  macro_events  — FOMC announcement + BLS CPI datetimes (UTC). These are public
                  and deterministic; curated by hand from the Fed/BLS schedules.
                  This script only VALIDATES/normalises them (sorts, checks tz).

  token_unlocks — per-coin unlock datetimes, e.g. {"ARB/USDT": ["2026-07-16T00:00:00Z"]}.
                  Deliberately left empty in the repo: reliable point-in-time unlock
                  data is paywalled (DefiLlama emissions returns HTTP 402; token.unlocks
                  has no free API). Wire fetch_unlocks() to whatever feed you license
                  rather than hand-guessing dates — a veto on wrong dates is worse than
                  no veto.

Usage:
    python3 scripts/update_event_blackouts.py --validate
    python3 scripts/update_event_blackouts.py --refresh-unlocks   # needs a feed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import datetime as dt

CALENDAR = Path(__file__).resolve().parents[1] / "user_data" / "gap_analysis" / "event_blackouts.json"
WHITELIST = [  # the curated universe the veto applies to
    "ADA/USDT", "AVAX/USDT", "BCH/USDT", "BTC/USDT", "DOGE/USDT", "ENA/USDT",
    "ETH/USDT", "FET/USDT", "ICP/USDT", "INJ/USDT", "LINK/USDT", "LTC/USDT",
    "MEME/USDT", "NEAR/USDT", "ONDO/USDT", "PEPE/USDT", "RENDER/USDT", "SOL/USDT",
    "SUI/USDT", "TAO/USDT", "TON/USDT", "TRX/USDT", "XLM/USDT", "XRP/USDT", "ZEC/USDT",
]


def load() -> dict:
    return json.loads(CALENDAR.read_text())


def validate(cal: dict) -> int:
    problems = 0
    macro = cal.get("macro_events", [])
    for ev in macro:
        try:
            dt.datetime.fromisoformat(ev["datetime"].replace("Z", "+00:00"))
        except Exception as e:  # noqa: BLE001
            print(f"  BAD macro datetime {ev!r}: {e}"); problems += 1
    for pair, dates in cal.get("token_unlocks", {}).items():
        if pair not in WHITELIST:
            print(f"  WARN unlock for non-whitelisted pair: {pair}")
        for d in dates:
            try:
                dt.datetime.fromisoformat(d.replace("Z", "+00:00"))
            except Exception as e:  # noqa: BLE001
                print(f"  BAD unlock datetime {pair} {d!r}: {e}"); problems += 1
    # normalise: sort macro events chronologically
    macro.sort(key=lambda e: e["datetime"])
    print(f"  {len(macro)} macro events, "
          f"{sum(len(v) for v in cal.get('token_unlocks', {}).values())} unlock dates, "
          f"{problems} problem(s)")
    return problems


def fetch_unlocks() -> dict:
    """Return {pair: [iso_datetime, ...]} from a licensed unlock feed.

    Not implemented: hook your data source here (e.g. DefiLlama emissions with an
    API key in $DEFILLAMA_KEY, or a Messari/CryptoRank endpoint). Map each token to
    its PAIR/USDT symbol and emit UTC ISO datetimes.
    """
    key = os.environ.get("DEFILLAMA_KEY") or os.environ.get("UNLOCKS_API_KEY")
    if not key:
        raise SystemExit(
            "No unlock-feed API key found ($DEFILLAMA_KEY / $UNLOCKS_API_KEY). "
            "Token-unlock data is paywalled; wire fetch_unlocks() to your feed. "
            "Leaving token_unlocks empty (macro-event veto still active)."
        )
    raise NotImplementedError("Plug your licensed unlock feed into fetch_unlocks().")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--refresh-unlocks", action="store_true")
    args = ap.parse_args()
    cal = load()

    if args.refresh_unlocks:
        cal["token_unlocks"] = fetch_unlocks()

    problems = validate(cal)
    if args.validate and not args.refresh_unlocks:
        sys.exit(1 if problems else 0)

    CALENDAR.write_text(json.dumps(cal, indent=2) + "\n")
    print(f"Wrote {CALENDAR}")


if __name__ == "__main__":
    main()
