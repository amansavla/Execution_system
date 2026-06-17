#!/usr/bin/env python3
"""Fetch the COMPLETE RTH 1-min underlying series for a date from IBKR
historical data and persist it as the authoritative replay source.

Why: data/bars/bars_<date>.jsonl is built LIVE, so it only covers the
period after the runner connected. If the runner starts mid-session (a
recurring situation), the morning is missing — and with it the breakout
9:30 reference and any morning decision's bars. Shadow-replay needs the
full, gap-free day, exactly like a backtest. This pulls 09:30-16:00 ET
1-min TRADES bars and writes:

    data/bars/underlying_<date>.jsonl   (one JSON object per minute)
        {"symbol","minute_ny","open","high","low","close"}

Deduped and sorted by minute. Safe to re-run (overwrites the file).

Usage:
    python3 scripts/backfill_underlying.py [YYYYMMDD ...]   # default: today (NY)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1]))

from ib_async import IB, Index

NY = ZoneInfo("America/New_York")
OUT_DIR = Path("data/bars")


async def fetch_day(ib: IB, contract, day: str) -> list[dict]:
    """Fetch 09:30-16:00 ET 1-min TRADES bars for YYYYMMDD."""
    today_ny = datetime.now(NY).strftime("%Y%m%d")
    if day == today_ny:
        # 16:00 close is in the future intraday — end at "now" (RTH so far).
        end_str = ""
    else:
        # IBKR UTC dash format: yyyymmdd-hh:mm:ss (16:00 ET -> UTC).
        d_utc = datetime.strptime(day, "%Y%m%d").replace(
            hour=16, minute=0, tzinfo=NY).astimezone(UTC)
        end_str = d_utc.strftime("%Y%m%d-%H:%M:%S")
    bars = await ib.reqHistoricalDataAsync(
        contract, endDateTime=end_str, durationStr="1 D",
        barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, formatDate=2,
    )
    rows: dict[str, dict] = {}
    for b in bars:
        t = b.date.astimezone(NY) if hasattr(b.date, "astimezone") else b.date
        if t.strftime("%Y%m%d") != day:
            continue  # IBKR can spill adjacent sessions
        minute_ny = t.replace(second=0, microsecond=0).isoformat()
        rows[minute_ny] = {
            "symbol": "XSP", "minute_ny": minute_ny,
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
        }
    return [rows[k] for k in sorted(rows)]


async def main(days: list[str]) -> None:
    ib = IB()
    await ib.connectAsync("127.0.0.1", 7497, clientId=78)
    c = Index("XSP", "CBOE", "USD")
    await ib.qualifyContractsAsync(c)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for day in days:
        try:
            rows = await fetch_day(ib, c, day)
        except Exception as e:
            print(f"{day}: FETCH FAILED — {e}")
            continue
        if not rows:
            print(f"{day}: no bars returned")
            continue
        out = OUT_DIR / f"underlying_{day}.jsonl"
        with open(out, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        first, last = rows[0]["minute_ny"][11:16], rows[-1]["minute_ny"][11:16]
        print(f"{day}: wrote {len(rows)} bars {first}-{last} -> {out}")
    ib.disconnect()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = [datetime.now(NY).strftime("%Y%m%d")]
    asyncio.run(main(args))
