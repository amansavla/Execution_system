#!/usr/bin/env python3
"""One-shot backfill: copy realized PnL from position_closed/forced events
into position_attribution rows that are missing it.

Why: close-time upserts were dropped under SQLite contention before the
retry fix (2026-06-11), and restart sweeps close rows without PnL — so the
dashboard's all-time PnL and history showed $0 everywhere. The event log
has the authoritative realized_pnl for every close; this replays it.

Also dedupes attribution rows left behind by the pre-fix seeding bug
(each restart inserted a fresh row per held contract): rows with qty=0
that never carried PnL are deleted.

Usage: python3 scripts/backfill_pnl.py [db_path]   (default data/events.db)
"""

import json
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "data/events.db"
conn = sqlite3.connect(db_path, timeout=30.0)
conn.execute("PRAGMA busy_timeout=30000;")

events = conn.execute(
    "SELECT event_type, payload FROM events WHERE event_type IN "
    "('position_closed', 'position_forced_closed') ORDER BY timestamp ASC"
).fetchall()

updated = 0
for _etype, payload in events:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        continue
    pid = data.get("position_id")
    pnl = data.get("realized_pnl")
    ts = data.get("timestamp")
    if pid is None or pnl is None:
        continue
    cur = conn.execute(
        "UPDATE position_attribution SET realized_pnl=?, "
        "closed_at=COALESCE(closed_at, ?) "
        "WHERE position_id=? AND (realized_pnl IS NULL OR realized_pnl = 0)",
        (float(pnl), ts, str(pid)),
    )
    updated += cur.rowcount

# Phantom rows from the zero-qty seeding bug: never traded, no PnL
cur = conn.execute(
    "DELETE FROM position_attribution WHERE quantity = 0 "
    "AND (realized_pnl IS NULL OR realized_pnl = 0) AND close_reason IS NULL"
)
deleted = cur.rowcount

conn.commit()
print(f"backfilled realized_pnl on {updated} rows; deleted {deleted} phantom rows")

rows = conn.execute(
    "SELECT strategy_id, COUNT(*), ROUND(SUM(COALESCE(realized_pnl,0)),2) "
    "FROM position_attribution WHERE status IN ('CLOSED','closed') "
    "GROUP BY strategy_id"
).fetchall()
for r in rows:
    print(f"  {r[0]:32s} closed={r[1]:3d} realized={r[2]}")
conn.close()
