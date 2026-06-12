# Runbook — Operating the Execution System

> Post-incident behavior changes (2026-06-11) are documented in
> [INCIDENTS_2026-06-11.md](INCIDENTS_2026-06-11.md): chain-aware leg
> coordination, stuck-cancel sweep, entry-window gating, persistent
> traded-today, batched event writes, exit staleness/spread budgets,
> marketable-plus exit repricing, rotating logs.

## Start (supervised — recommended)

```bash
cd "/Users/aman/US strats/Execution_System_v2"
nohup ./scripts/run_supervised.sh >> data/supervisor.log 2>&1 &
DASHBOARD_DB=data/events.db nohup python3 -m uvicorn src.dashboard.app:app --port 8500 >> data/dashboard.log 2>&1 &
```
Dashboard: http://localhost:8500. The supervisor relaunches the runner
after crashes or a dashboard Restart; dashboard Shutdown stops it for good.

Prereqs: TWS (paper, port 7497) logged in with API enabled.
`configs/broker.yaml` client_id must not collide with other sessions.

## Stop

Dashboard → System → Shutdown (graceful: supervisor exits too). Manual:
`pkill -f run_paper_trading.py` (positions stay at the broker; next start
re-seeds them with correct strategies from position_attribution).

## The system says LOCKED — what now?

Locked = PROTECT MODE, not frozen: exits/stops still run, commands still
work, entries are blocked. Steps:
1. Read the Errors panel / `grep -iE "lock|reject|mismatch" data/runner.log | tail`.
2. If positions look wrong → fix at the broker or use per-position Exit /
   Flatten All from the dashboard (these work while locked).
3. Dashboard → System → **Unlock**. It reconciles first and only unlocks
   clean; if it fails the result tooltip says why.
4. Disconnect-caused locks auto-resume on their own once TWS is back.

## Common operations

| Task | How |
|---|---|
| Exit one position | Positions table → Exit |
| Flatten everything | Controls → Flatten All (latches until flat) |
| Pause / resume a strategy | Strategy controls card |
| Enable/disable a strategy | Strategy Management table → ON/OFF |
| Change SL %, time exit, sizing, trigger | Strategy Management row → edit → Apply (live + persisted to configs/strategy_overrides.yaml) |
| Cancel a working order | Orders table → Cancel |
| Restart the runner | System → Restart (supervisor relaunches) |

## Daily checks

- `python3 scripts/shadow_replay.py YYYYMMDD` after the close →
  `reports/shadow_*.md` diffs live fills vs notebook logic.
- `sqlite3 data/events.db "SELECT json_extract(payload,'$.strategy_id'),
  json_extract(payload,'$.slippage_vs_first_limit'),
  json_extract(payload,'$.time_to_fill_seconds') FROM events WHERE
  event_type='execution_quality' AND timestamp>=date('now');"` —
  slippage and time-to-fill per fill.

## Tests

```bash
python3 -m pytest tests/unit -q                       # fast (~3s)
python3 -m pytest tests/unit tests/integration -q \
  --ignore=tests/integration/test_ibkr_paper_connection.py   # full offline
python3 -m pytest tests/integration/test_ibkr_paper_connection.py -q  # needs TWS
```

## Off-hours broker validation

```bash
python3 scripts/live_cancel_shakeout.py   # needs TWS; uses client_id 17
```
Places unfillable deep-OTM $0.01 limits and asserts: place→ack,
cancel→confirmation, cancel/replace chain, ground-truth status query.
Safe to run any time TWS is up — it cannot fill or affect the runner
(separate client id). For full entry→exit cycling during RTH, enable
`shakeout_cycle_test` in `configs/strategies.yaml` (1-lot, defined risk).

## Known sharp edges

- Adaptive algo is RTH-only; outside 9:30–16:00 NY orders go as plain
  limits automatically (incl. GTH overnight sessions).
- IBKR rejects in-place modification of Adaptive-algo orders — keep
  `use_in_place_modify: False` (the repricer cancel/replaces instead).
- `exit.time_exit_utc` in strategies.yaml is interpreted in
  America/New_York (legacy field name). Entries are blocked once the
  exit time has passed.
- Base-config edits (risk.yaml etc.) need a runner Restart; only the
  Strategy Management whitelist applies live.
- Exit-rule changes apply to NEW positions; open positions keep the
  rules they were opened with.
- Never run two runners against the same paper account (position
  adoption conflicts); client_id must differ from any other API session.
