#!/usr/bin/env python3
"""Shadow replay acceptance test — CLI wrapper.

Usage:
    python3 scripts/shadow_replay.py [YYYYMMDD] [--db data/events.db]

Replays data/bars/bars_<date>.jsonl through the notebook logic for every
ENABLED breakout strategy in configs/strategies.yaml, diffs against the
actual fills recorded in the events DB, and writes:
    reports/shadow_<date>.md        human-readable report
    reports/shadow_<date>.diff.log  machine-readable diff (JSONL)
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import yaml

from src.replay.shadow import (
    diff_trades,
    load_actual_fills,
    load_bars,
    replay_breakout,
    write_report,
)
from src.strategies.xsp_breakout import STRATEGY_PARAMS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=datetime.now(UTC).strftime("%Y%m%d"))
    ap.add_argument("--db", default="data/events.db")
    ap.add_argument("--bars-dir", default="data/bars")
    ap.add_argument("--configs", default="configs/strategies.yaml")
    args = ap.parse_args()

    bars_path = Path(args.bars_dir) / f"bars_{args.date}.jsonl"
    bars = load_bars(bars_path)
    if not bars:
        print(f"No bars recorded at {bars_path} — nothing to replay.")
        return 1

    underlying = bars.get("XSP", [])
    option_bars = {k: v for k, v in bars.items() if k != "XSP"}
    print(f"Loaded bars: XSP={len(underlying)}, option symbols={len(option_bars)}")

    cfg = yaml.safe_load(Path(args.configs).read_text()) or {}
    fills = load_actual_fills(args.db)
    print(f"Actual fills (execution_quality): {len(fills)}")

    replays, diffs, notes = [], [], []
    notes.append("Straddle entry strike selection is not replayable from bars "
                 "(needs the full quote surface); straddle legs are diffed on "
                 "exits only when their bars were recorded.")

    for s in cfg.get("strategies", []):
        sid = s.get("strategy_id", "")
        if not s.get("enabled") or sid not in STRATEGY_PARAMS:
            continue
        p = STRATEGY_PARAMS[sid]
        sl = (s.get("exit") or {}).get("stop_loss_pct", 30) / 100.0
        t = replay_breakout(underlying, option_bars, sid,
                            p["hour"], p["minute"], p["trigger_pct"], sl)
        if t is None:
            print(f"{sid}: no signal in recorded bars")
            continue
        replays.append(t)
        strat_fills = [f for f in fills if f.get("strategy_id") == sid]
        diffs.extend(diff_trades(t, strat_fills))

    date_fmt = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}"
    report, difflog = write_report(date_fmt, replays, diffs, notes=notes)
    print(f"Report:   {report}")
    print(f"Diff log: {difflog}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
