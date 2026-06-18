# Runbook

This is the operator path for paper trading.

## Start

Prerequisites:

- TWS or IB Gateway is logged into a paper account.
- API access is enabled.
- `configs/broker.yaml` uses the paper port, usually `7497`.
- `live_trading.enabled` is `false`.

Start the runner under the supervisor:

```bash
nohup ./scripts/run_supervised.sh >> data/supervisor.log 2>&1 &
```

Start the dashboard:

```bash
DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app --port 8500
```

Dashboard:

```text
http://localhost:8500
```

## Stop

Preferred: use the dashboard shutdown control.

Manual fallback:

```bash
pkill -f run_paper_trading.py
```

Runner shutdown is not a trading instruction. Confirm open positions and
orders in TWS after any manual stop.

## Locked Mode

Locked mode blocks new entries. It should still allow exits, flatten
commands, cancel commands, state publishing, and reconciliation.

When the system is locked:

1. Check the dashboard error/status panels.
2. Inspect recent logs:

   ```bash
   grep -iE "lock|reject|mismatch|disconnect" data/runner.log | tail
   ```

3. Resolve broker/internal mismatches or flatten exposure.
4. Use dashboard unlock. Unlock is reconciliation-gated and should fail
   if state is still inconsistent.

## Common Tasks

| Task | Preferred path |
| --- | --- |
| Exit one position | Dashboard positions table |
| Flatten all positions | Dashboard controls |
| Cancel one order | Dashboard orders table |
| Pause or resume a strategy | Dashboard strategy controls |
| Lock or unlock the system | Dashboard system controls |
| Restart runner | Dashboard restart or supervised script |

## Daily Checks

Run tests before changing runtime config:

```bash
python3 -m pytest tests/unit -q
python3 -m pytest tests/unit tests/integration -q --ignore=tests/integration/test_ibkr_paper_connection.py
```

After the trading session, review:

- dashboard closed positions
- realized PnL attribution
- rejected orders
- reconciliation events
- `data/runner.log`
- `data/events.db`

## Known Edges

- `exit.time_exit_utc` is a legacy field name and is interpreted as
  America/New_York local time in current docs/code paths.
- Base config changes require a runner restart.
- Dashboard strategy edits persist through `configs/strategy_overrides.yaml`.
- Do not run two runner processes against the same account.
- Do not enable live trading from the paper runner.

