# Dashboard API Reference

Complete API documentation for manual control and monitoring.

## Overview

The dashboard is a read-mostly HTTP API with limited write operations for manual control. All writes go through SQLite's `commands` table, which the runner drains.

```
Dashboard (Browser at :8500)
    ↓
FastAPI Server
    ├─ Read endpoints (GET)     → SQLite queries (no runner involvement)
    ├─ Write endpoints (POST)   → CommandQueue.enqueue() → SQLite commands table
    └─ WebSocket /ws           → Live state push (1s cadence)
```

---

## Read Endpoints (GET)

### `GET /` — Serve UI

Returns `index.html` with `Cache-Control: no-cache, must-revalidate` header.

**Response:** HTML page (browser renders it)

---

### `GET /api/state` — Runtime Snapshot

Returns current system state: positions, orders, strategy status, PnL.

```bash
curl http://localhost:8500/api/state | jq .
```

**Response:**
```json
{
  "status": "running",
  "timestamp": "2026-06-12T10:35:42.123Z",
  
  "positions": [
    {
      "position_id": "uuid-abc123",
      "strategy_id": "xsp_straddle_1100_40",
      "contract": {
        "symbol": "XSP",
        "expiry": "20260612",
        "strike": 740.0,
        "right": "CALL"
      },
      "side": "SELL",
      "quantity": -5,
      "average_entry_price": 2.50,
      "entry_time": "2026-06-12T10:30:00Z",
      "status": "OPEN",
      "current_mark": 2.45,
      "unrealized_pnl": 25.00,  # (2.50 - 2.45) * 100 * 5
      "stop_loss_pct": 40.0,
      "exit_time_utc": "19:30:00"
    }
  ],
  
  "orders": [
    {
      "order_id": "uuid-order-123",
      "strategy_id": "xsp_straddle_1100_40",
      "contract": "XSP 20260612 740.0 CALL",
      "side": "SELL",
      "qty": 5,
      "limit_price": 2.50,
      "state": "WORKING",
      "submitted_at": "2026-06-12T10:30:00Z",
      "time_to_live": "60s",
      "fill_price": null,
      "fill_qty": 0
    }
  ],
  
  "strategy_pnl": {
    "xsp_straddle_1100_40": {
      "unrealized": 125.00,
      "realized_today": 450.50,
      "total": 575.50
    }
  },
  
  "system": {
    "locked": false,
    "locked_reason": null,
    "traded_today": ["xsp_straddle_1100_40"],
    "allow_reentry": {
      "xsp_straddle_1100_40": true,
      "xsp_breakout_0945": false
    }
  }
}
```

**Usage:** Dashboard auto-updates via WebSocket (below). Use `/api/state` for initial page load or REST-only clients.

---

### `GET /api/pnl` — Daily & All-Time PnL Breakdown

Returns comprehensive PnL summary with daily breakdown and per-strategy details.

```bash
curl http://localhost:8500/api/pnl | jq .
```

**Response:**
```json
{
  "strategies": {
    "xsp_straddle_1100_40": {
      "realized_all_time": 1250.50,
      "realized_today": 450.50,
      "unrealized_live": -75.25,
      "total": 1175.25,
      "closed_positions": 8
    },
    "xsp_breakout_0945": {
      "realized_all_time": 320.00,
      "realized_today": 0.00,
      "unrealized_live": 50.00,
      "total": 370.00,
      "closed_positions": 3
    }
  },
  
  "total_pnl": 1545.25,
  
  "today": {
    "date": "2026-06-12",
    "realized": 450.50,
    "unrealized": -25.25,
    "total": 425.25,
    "closed_positions": 11,
    "per_strategy": {
      "xsp_straddle_1100_40": 450.50,
      "xsp_breakout_0945": 0.00
    }
  },
  
  "daily": [
    {
      "date": "2026-06-12",
      "realized_pnl": 450.50,
      "closed_positions": 11
    },
    {
      "date": "2026-06-11",
      "realized_pnl": -125.00,
      "closed_positions": 5
    },
    {
      "date": "2026-06-10",
      "realized_pnl": 800.50,
      "closed_positions": 8
    }
  ]
}
```

**Note:** Dates are America/New_York calendar dates (market close date), not UTC.

---

### `GET /api/history?limit=100` — Closed Positions

Returns recent closed positions with realized PnL and close reason.

```bash
curl "http://localhost:8500/api/history?limit=50" | jq .
```

**Response:**
```json
[
  {
    "strategy_id": "xsp_straddle_1100_40",
    "symbol": "XSP",
    "expiry": "20260612",
    "strike": 740.0,
    "right": "CALL",
    "side": "SELL",
    "quantity": 5,
    "avg_entry_price": 2.50,
    "realized_pnl": 75.00,
    "close_reason": "TIME_EXIT",
    "entry_time": "2026-06-12T10:30:00Z",
    "closed_at": "2026-06-12T15:30:05Z"
  },
  {
    "strategy_id": "xsp_straddle_1100_40",
    "symbol": "XSP",
    "expiry": "20260612",
    "strike": 750.0,
    "right": "PUT",
    "side": "SELL",
    "quantity": 5,
    "avg_entry_price": 2.25,
    "realized_pnl": -50.00,
    "close_reason": "STOP_LOSS",
    "entry_time": "2026-06-12T11:00:00Z",
    "closed_at": "2026-06-12T11:15:30Z"
  }
]
```

**Close Reasons:**
- `TIME_EXIT` — Closed by time_exit_utc time gate
- `STOP_LOSS` — Closed by stop_loss_pct breach
- `MANUAL` — Manually closed via dashboard command
- `SYSTEM_LOCK` — Closed due to reconciliation failure or system error

---

### `GET /api/events?limit=50` — Event Log

Recent events from the runner's event store. Read-only.

```bash
curl "http://localhost:8500/api/events?limit=20" | jq .
```

**Response:**
```json
[
  {
    "timestamp": "2026-06-12T10:35:42.123Z",
    "event_type": "FILL",
    "strategy_id": "xsp_straddle_1100_40",
    "payload": {
      "order_id": "uuid-order-123",
      "fill_price": 2.49,
      "fill_qty": 5,
      "contract": "XSP 20260612 740.0 CALL SELL"
    }
  },
  {
    "timestamp": "2026-06-12T10:35:35.456Z",
    "event_type": "REPRICE",
    "strategy_id": "xsp_straddle_1100_40",
    "payload": {
      "order_id": "uuid-order-123",
      "old_price": 2.51,
      "new_price": 2.49,
      "quote_bid": 2.48,
      "quote_ask": 2.50
    }
  },
  {
    "timestamp": "2026-06-12T10:30:00.001Z",
    "event_type": "SUBMIT",
    "strategy_id": "xsp_straddle_1100_40",
    "payload": {
      "order_id": "uuid-order-123",
      "contract": "XSP 20260612 740.0 CALL SELL",
      "qty": 5,
      "limit_price": 2.50
    }
  }
]
```

**Event Types:**
- `SUBMIT` — Order submitted
- `REPRICED` — Order price updated
- `FILL` — Partial or full fill
- `CANCEL_REQUESTED` — Cancel requested
- `CANCELLED` — Cancel confirmed
- `REJECTED` — Order rejected
- `TIMEOUT` — Order TTL expired
- `CLOSE` — Position closed
- `RECON_FAILED` — Reconciliation failure

---

### `GET /api/commands?limit=50` — Command History

Recent commands executed (manual control operations).

```bash
curl "http://localhost:8500/api/commands?limit=10" | jq .
```

**Response:**
```json
[
  {
    "command_id": "uuid-cmd-123",
    "type": "close_position",
    "payload": {
      "position_id": "uuid-pos-456",
      "reason": "MANUAL"
    },
    "status": "EXECUTED",
    "enqueued_at": "2026-06-12T10:35:00Z",
    "executed_at": "2026-06-12T10:35:01Z"
  },
  {
    "command_id": "uuid-cmd-122",
    "type": "unlock_system",
    "payload": {},
    "status": "EXECUTED",
    "enqueued_at": "2026-06-12T10:25:00Z",
    "executed_at": "2026-06-12T10:25:02Z"
  }
]
```

---

### `GET /api/logs?lines=100&errors_only=false` — Runner Log Tail

Tail of runner.log (file-based, read-only).

```bash
curl "http://localhost:8500/api/logs?lines=20" | jq .
```

**Response:**
```json
[
  "2026-06-12 10:35:42,123 [INFO] src.app.runner: Reconciliation passed cleanly. Matches: 2",
  "2026-06-12 10:35:35,456 [INFO] src.execution.order_manager: Order 123 repriced: 2.51 → 2.49",
  "2026-06-12 10:35:30,789 [WARNING] src.broker.ibkr_broker: Quote staleness 6.5s (exceeds 5.0s max)",
  "2026-06-12 10:30:00,001 [INFO] src.app.runner: Submitted order: XSP 20260612 740 CALL SELL qty=5"
]
```

**Parameters:**
- `lines=100` — Number of lines to return (default 100, max 500)
- `errors_only=true` — Only include ERROR/WARNING/CRITICAL

---

### `WS /ws` — WebSocket Live Updates

Real-time state push (1s cadence). Browser connects and receives live updates.

**URL:** `ws://localhost:8500/ws`

**Frame (every 1 second):**
```json
{
  "status": "running",
  "timestamp": "2026-06-12T10:35:42.123Z",
  "positions": [...],
  "orders": [...],
  "strategy_pnl": {...},
  "system": {...}
}
```

Same schema as `GET /api/state`. Useful for:
- Live position PnL tracking
- Real-time order status
- Order fill notifications

**Browser Usage:**
```javascript
const ws = new WebSocket('ws://localhost:8500/ws');
ws.onmessage = (event) => {
    const state = JSON.parse(event.data);
    console.log('Live position PnL:', state.strategy_pnl);
};
```

---

## Write Endpoints (POST)

### `POST /api/commands` — Enqueue Manual Command

Submit a command for the runner to execute. Runner drains this table every tick.

```bash
curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{
    "type": "close_position",
    "payload": {
      "position_id": "uuid-pos-123",
      "reason": "MANUAL"
    }
  }'
```

**Response:**
```json
{
  "command_id": "uuid-cmd-789",
  "status": "pending",
  "enqueued_at": "2026-06-12T10:35:00Z"
}
```

**Valid Command Types:**

#### `close_position` — Close a specific position

```json
{
  "type": "close_position",
  "payload": {
    "position_id": "uuid-pos-123"
  }
}
```

Runner immediately submits exit order at market.

#### `close_strategy` — Close all positions for a strategy

```json
{
  "type": "close_strategy",
  "payload": {
    "strategy_id": "xsp_straddle_1100_40"
  }
}
```

Closes all open positions belonging to this strategy.

#### `close_all` — Close all open positions

```json
{
  "type": "close_all",
  "payload": {}
}
```

Emergency exit: closes everything at market.

#### `unlock_system` — Clear reconciliation lock

```json
{
  "type": "unlock_system",
  "payload": {}
}
```

Used after operator has manually synced state and wants to re-enable trading. See [RUNBOOK.md](RUNBOOK.md) for detailed steps.

#### `enable_reentry` / `disable_reentry` — Per-strategy re-entry control

```json
{
  "type": "enable_reentry",
  "payload": {
    "strategy_id": "xsp_straddle_1100_40"
  }
}
```

Allows a strategy to trade again same day (if already traded). Useful for multi-cycle strategies.

#### `pause_strategy` — Temporarily disable a strategy

```json
{
  "type": "pause_strategy",
  "payload": {
    "strategy_id": "xsp_straddle_1100_40"
  }
}
```

Strategy won't generate new entry signals until resumed.

#### `resume_strategy` — Resume a paused strategy

```json
{
  "type": "resume_strategy",
  "payload": {
    "strategy_id": "xsp_straddle_1100_40"
  }
}
```

---

## Error Handling

All endpoints return `200 OK` or `400 Bad Request`:

```bash
# Bad command type
curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{"type": "invalid_command", "payload": {}}'

# Response:
{
  "detail": "unknown command type: invalid_command"
}
```

---

## Rate Limits & Performance

**No rate limiting** (all requests allowed).

**DB Timeouts:**
- Reads: 5s timeout (SQLite WAL mode keeps readers non-blocking)
- Writes: 5s timeout with retry on lock (CommandQueue)

**Performance Notes:**
- `/api/state`: ~10-50ms (snapshots all data)
- `/api/pnl`: ~20-100ms (aggregates 30 days of data)
- `/api/history`: ~30-200ms (depends on limit)
- `/api/events`: ~10ms (limited to 50 events)

If dashboard feels slow:
- Check runner logs for "database is locked" errors
- Reduce `/api/pnl` lookback days
- Lower `/api/history` limit

---

## Common Workflows

### Close a Losing Position

```javascript
// Get current state
const state = await fetch('/api/state').then(r => r.json());

// Find the position
const losing = state.positions.filter(p => p.unrealized_pnl < 0)[0];

// Submit close command
await fetch('/api/commands', {
  method: 'POST',
  body: JSON.stringify({
    type: 'close_position',
    payload: { position_id: losing.position_id }
  })
});
```

### Check Daily PnL

```javascript
const pnl = await fetch('/api/pnl').then(r => r.json());
console.log(`Today: ${pnl.today.total}`);
console.log(`Breakdown:`, pnl.today.per_strategy);
```

### Monitor Strategy Performance

```javascript
const state = await fetch('/api/state').then(r => r.json());
state.positions
  .filter(p => p.strategy_id === 'xsp_straddle_1100_40')
  .forEach(p => console.log(`${p.contract.strike}: ${p.unrealized_pnl}`));
```

### Emergency Exit

```javascript
await fetch('/api/commands', {
  method: 'POST',
  body: JSON.stringify({ type: 'close_all', payload: {} })
});
```

---

## Integration Examples

### Python Client

```python
import requests
import json

class DashboardClient:
    def __init__(self, base_url='http://localhost:8500'):
        self.base_url = base_url
    
    def get_state(self):
        return requests.get(f'{self.base_url}/api/state').json()
    
    def get_pnl(self):
        return requests.get(f'{self.base_url}/api/pnl').json()
    
    def close_position(self, position_id):
        return requests.post(
            f'{self.base_url}/api/commands',
            json={'type': 'close_position', 'payload': {'position_id': position_id}}
        ).json()
    
    def close_all(self):
        return requests.post(
            f'{self.base_url}/api/commands',
            json={'type': 'close_all', 'payload': {}}
        ).json()

# Usage
client = DashboardClient()
state = client.get_state()
print(f"Open positions: {len(state['positions'])}")

# Emergency close
client.close_all()
```

### JavaScript (Fetch)

```javascript
const client = {
    async getState() {
        return fetch('http://localhost:8500/api/state').then(r => r.json());
    },
    
    async getPnL() {
        return fetch('http://localhost:8500/api/pnl').then(r => r.json());
    },
    
    async closePosition(positionId) {
        return fetch('http://localhost:8500/api/commands', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'close_position',
                payload: { position_id: positionId }
            })
        }).then(r => r.json());
    }
};

// Usage
const state = await client.getState();
console.log(`Total PnL: ${state.strategy_pnl}`);
```

---

## Debugging

### Dashboard Not Updating

```bash
# Check runner is writing events
tail -f data/runner.log | grep -i "event\|fill"

# Check database exists and is accessible
ls -la data/events.db
sqlite3 data/events.db "SELECT COUNT(*) FROM events;"

# Check dashboard has database path correct
curl http://localhost:8500/api/state
```

### Slow API Responses

```bash
# Check for database locks
sqlite3 data/events.db "PRAGMA busy_timeout=10000; SELECT * FROM events LIMIT 1;"

# Check runner isn't hogging CPU
top -p $(pgrep -f "src.app.runner")
```

### Commands Not Executing

```bash
# Check command in queue
sqlite3 data/events.db "SELECT * FROM commands WHERE status='pending';"

# Check runner logs for execution errors
tail -f data/runner.log | grep "command"
```
