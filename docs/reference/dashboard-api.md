# Dashboard API

The FastAPI dashboard is a database-only control plane. It reads SQLite
state and enqueues commands for the runner. It must not import or call the
broker.

Default local URL:

```text
http://localhost:8500
```

## Run

```bash
DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app --port 8500
```

Optional environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DASHBOARD_DB` | `data/events.db` | SQLite database used by runner and dashboard |
| `RUNNER_LOG` | `data/runner.log` | Log file tailed by `/api/logs` |

## Read Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Serve the static dashboard UI |
| `GET` | `/api/state` | Current runtime snapshot |
| `GET` | `/api/events?limit=50` | Recent event log rows |
| `GET` | `/api/commands?limit=50` | Recent command queue rows |
| `GET` | `/api/logs?lines=100&errors_only=false` | Tail runner logs |
| `GET` | `/api/pnl` | Realized and live PnL summary |
| `GET` | `/api/history?limit=100` | Recently closed positions |
| `WS` | `/ws` | Runtime snapshot pushed once per second |

Example:

```bash
curl http://localhost:8500/api/state
curl "http://localhost:8500/api/events?limit=25"
```

## Write Endpoint

All writes use:

```text
POST /api/commands
```

Request body:

```json
{
  "type": "lock_system",
  "payload": {}
}
```

Response:

```json
{
  "command_id": "<uuid>",
  "status": "pending"
}
```

## Command Types

| Command | Payload |
| --- | --- |
| `exit_position` | `{"position_id": "<uuid>"}` |
| `cancel_order` | `{"order_id": "<uuid>"}` |
| `pause_strategy` | `{"strategy_id": "...", "paused": true}` |
| `pause_strategy` | `{"strategy_id": "...", "paused": false}` |
| `flatten_all` | `{}` |
| `update_strategy` | strategy parameter payload |
| `unlock_system` | `{}` |
| `lock_system` | `{}` |
| `restart_runner` | `{}` |
| `shutdown_runner` | `{}` |

Examples:

```bash
curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{"type":"lock_system","payload":{}}'

curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{"type":"exit_position","payload":{"position_id":"<uuid>"}}'

curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{"type":"flatten_all","payload":{}}'
```

## Command Lifecycle

Commands are durable rows in SQLite:

```text
pending -> done
pending -> failed
```

The dashboard only creates commands. The runner decides how to execute
them and records the terminal result.

## Safety Expectations

- Flatten and exit actions must remain available while the system is
  locked.
- Unlock must be reconciliation-gated.
- Cancel actions route through `OrderManager`.
- Dashboard actions should require explicit confirmation for destructive
  operations.
- Every manual action should be visible through command history.

The legacy CLI in `src/app/control.py` still exposes direct
`ManualControlService` operations. Prefer the dashboard command queue for
operator workflows until the CLI is reconciled with the same control
plane.

