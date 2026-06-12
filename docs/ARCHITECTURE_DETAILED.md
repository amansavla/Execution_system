# Execution System v2 — Detailed Architecture Guide

## Table of Contents
1. [System Overview](#system-overview)
2. [Core Components](#core-components)
3. [Data Flow](#data-flow)
4. [Module Reference](#module-reference)
5. [Task Coordination](#task-coordination)
6. [Error Handling & Recovery](#error-handling--recovery)

---

## System Overview

The Execution System v2 is a real-time algorithmic options trading platform for 0DTE (zero-day-to-expiration) strategies. It operates as three coordinated processes:

```
┌─────────────────────────────────────────────────────────────┐
│                    IBKR TWS (Paper/Live)                    │
│                   Market Data + Order Routing                │
└──────────────────┬──────────────────────────────────────────┘
                   │ IBKRBroker (port 7497)
                   │
┌──────────────────▼──────────────────────────────────────────┐
│                      Runner Process                          │
│        (Execution logic, position management, exits)         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  EventStore    RuleEngine    PortfolioManager        │   │
│  │  (Events)      (Strategies)  (Positions, Recon)      │   │
│  └──────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  OrderManager     RiskEngine    ExitManager          │   │
│  │  (Order lifecycle)(Limits)      (Stops, time exits)  │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────┬──────────────────────────────────────────┘
                   │ SQLite WAL (events, commands, positions)
                   │
      ┌────────────┼────────────┐
      │            │            │
      ▼            ▼            ▼
   Dashboard    CommandQueue   PositionStore
   (UI/HTTP)    (Manual ops)   (Attribution)
```

### Key Principles

**Database-centric control plane:** The dashboard never talks to the broker. Commands flow through SQLite tables; runner drains them.

**Event sourcing:** All trades, fills, cancels logged as immutable events. Replay-safe state rebuilding.

**Fail-closed safety:** Reconciliation locks system on mismatch. Orders actively cancelled on shutdown. Orphaned positions tracked.

**Real-time repricing:** Orders repriced every 2s using in-place IBKR modify (no cancel/replace overhead).

---

## Core Components

### 1. **IBKRBroker** (`src/broker/ibkr_broker.py`)

**Purpose:** Two-way bridge to IBKR TWS (Interactive Brokers). Handles market data, order lifecycle, position reports.

**Responsibilities:**
- Subscribe to option chains (XSP, bid/ask updates)
- Route orders to IBKR (submit, cancel, modify)
- Consume fills and position updates
- Auto-reconnect on disconnect
- Manage contract objects (symbols → IBKR Contract specs)

**Key Methods:**

```python
class IBKRBroker:
    # Lifecycle
    connect()              # Establish TWS connection
    disconnect()           # Clean exit
    _auto_reconnect()      # Background reconnect task (held via strong ref)
    
    # Market data
    subscribe_chain(symbol, expiry)      # Get option chain for a date
    get_quote(contract)                  # Bid/ask snapshot
    
    # Orders
    submit_order(contract, side, qty, limit, ...)  # Place order
    cancel_order(order_id)                         # Request cancel
    modify_order(order_id, new_limit, new_qty)    # In-place modify
    
    # Fills & positions
    on_fill(order_id, fill_price, fill_qty)  # Callback on execution
    on_position(contract, qty, avg_price)    # Callback on position change
```

**State Machine (Order Lifecycle):**
```
PENDING_SUBMIT → WORKING ↔ BEING_MODIFIED → FILLED / CANCELLED / REJECTED
         ↓
    SUBMIT_REJECTED
```

**Paper Trading Quirks:**
- Fills are delayed and unreliable (expected behavior per IBKR)
- Some orders may never fill (especially deep OTM)
- Quotes can lag 1-2 seconds
- Adaptive algo cancels take 11-31 seconds to confirm (disabled in this config)

**Error Handling:**
```python
# If TWS drops, auto-reconnect fires (holds strong ref)
self._reconnect_task = asyncio.create_task(self._auto_reconnect())

# On disconnect: positions cached, orders marked UNKNOWN
# On reconnect: re-subscribe quotes, query open orders, resync state
```

---

### 2. **Runner** (`src/app/runner.py`)

**Purpose:** Central orchestration loop. Runs strategy logic, manages orders, enforces risk limits, handles exits.

**Architecture:**
```
┌─────────────────────────────────────────────┐
│  Runner._tick() [every 100ms]               │
├─────────────────────────────────────────────┤
│ 1. Poll strategies for entry signals        │
│ 2. Risk check (max contracts, premium)      │
│ 3. Submit new orders                        │
│ 4. Process fills (position seeding)         │
│ 5. Check stop-loss conditions               │
│ 6. Check time exits                         │
│ 7. Submit exit orders                       │
│ 8. Reprice working orders (2s cadence)      │
│ 9. Reconcile broker state                   │
│ 10. Publish runtime snapshot                │
└─────────────────────────────────────────────┘
```

**Entry Flow (High-Level):**

```python
def _tick(self):
    # 1. Get entry signals from strategies
    for strategy in self.strategies:
        signal = strategy.poll()  # Returns (side, qty, limit_price) or None
        if signal is None:
            continue
        
        # 2. Apply risk checks
        qty = self.risk_engine.compute_allowed_qty(signal)
        if qty == 0:
            continue
        
        # 3. Create multileg orders (e.g., straddle = BUY call + BUY put)
        orders = strategy.create_orders(qty, signal.limit_price)
        
        # 4. Submit to broker
        for order in orders:
            order_id = self.broker.submit_order(...)
            self.order_manager.register(order_id, order)
```

**Exit Flow (High-Level):**

```python
def _check_exits(self):
    for position in self.portfolio.open_positions:
        # Check stop-loss
        if position.pnl <= position.stop_loss_pct * position.entry_price:
            self._submit_exit(position, reason="STOP_LOSS")
        
        # Check time exit
        if now() >= position.time_exit_utc:
            self._submit_exit(position, reason="TIME_EXIT")
        
        # Check manual close
        if position in self.manual_close_set:
            self._submit_exit(position, reason="MANUAL")
```

**Exit With Retry Backoff:**

```python
def _submit_exit(self, position, reason):
    # Cancel any opposite-side working orders first
    self._cancel_opposite_side_orders(position.contract)
    
    # Try to submit exit
    try:
        order = self._create_exit_order(position)
        order_id = self.broker.submit_order(order)
    except RejectionError as e:
        if "Cannot have open orders on both sides" in str(e):
            # Backoff: 10s first time, then 60s after 5 attempts
            attempts = self._exit_attempts[position.id]
            if attempts[0] == 0:
                self._schedule_retry(position, delay=10)
            elif attempts[0] >= 5:
                operator_alert("Exit stuck on position X")
                self._schedule_retry(position, delay=60)
```

**Reconciliation (Fail-Closed):**

```python
def _reconcile(self):
    """Match broker positions vs internal portfolio state."""
    broker_positions = self.broker.get_positions()
    internal_positions = self.portfolio.open_positions
    
    matches = 0
    for bp in broker_positions:
        ip = self.portfolio.find(bp.contract, bp.side)
        if ip and ip.qty == bp.qty:
            matches += 1
        else:
            # MISMATCH: lock system, alert operator
            logger.critical(f"Recon failed: {bp} vs {ip}")
            self.override_manager.state.system_locked = True
            return False
    
    return True
```

**Key State:**

```python
class Runner:
    broker: IBKRBroker                          # Broker connection
    portfolio: PortfolioManager                 # Open positions
    order_manager: OrderManager                 # Order lifecycle tracking
    risk_engine: RiskEngine                     # Position size limits
    strategies: list[StrategyProvider]          # Entry signal generators
    override_manager: ManualControl             # Dashboard override controls
    
    # Restart safety
    _exit_attempts: dict[UUID, tuple[int, datetime]]  # Track exit retries
    _background_tasks: set[asyncio.Task]       # Hold reprice/cancel tasks
```

---

### 3. **OrderManager** (`src/execution/order_manager.py`)

**Purpose:** Tracks order state through its lifecycle and manages repricing.

**Order Lifecycle State Machine:**

```
SUBMITTED → WORKING ↔ BEING_REPRICED → FILLED / CANCELLED / REJECTED / TIMEOUT
    ↓
SUBMIT_REJECTED
```

**Key Responsibilities:**

1. **Track order state:** Maps order_id → Order object with timestamps, prices, qty
2. **Repricing:** Every 2s, check quote changes and submit in-place modify if needed
3. **Timeout enforcement:** Orders expire after 60s (configurable)
4. **Background task management:** Hold strong references to async reprice tasks

**Repricing Logic (In-Place Modify):**

```python
def _reprice_order_loop(self, order_id, config):
    """Continuously reprice order based on quote changes."""
    while True:
        order = self.orders[order_id]
        
        # Skip if order not working
        if order.state not in [OrderState.WORKING, OrderState.BEING_REPRICED]:
            return
        
        # Get current quote
        quote = self.broker.get_quote(order.contract)
        if quote is None or quote.staleness > config.max_quote_age:
            # Skip if quote stale (prevents using old data for SL)
            await asyncio.sleep(2)
            continue
        
        # Compute new price
        new_price = self._compute_new_price(order, quote)
        
        # If price changed >min_threshold, reprrice
        if abs(new_price - order.limit_price) >= config.min_reprice_threshold:
            if config.use_in_place_modify:
                # Single IBKR message, instant (no cancel wait)
                await self.broker.modify_order(
                    order_id, 
                    new_limit=new_price,
                    new_qty=order.qty
                )
            else:
                # Cancel and wait for ACK, then submit new order
                await self.broker.cancel_order(order_id)
                await self._wait_for_cancel(order_id, timeout=order.time_to_live)
                # New order submitted by retry logic
        
        await asyncio.sleep(2)  # 2s cadence
```

**Critical: Strong References to Tasks**

```python
def _spawn(self, coro):
    """Create task with strong reference (prevents GC)."""
    task = asyncio.create_task(coro)
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)
    return task

# Usage:
self._spawn(self._reprice_order_loop(order_id, config))
self._spawn(self.broker.cancel_order(order_id))
```

**Why This Matters:** Without strong references, Python's garbage collector can destroy the task before it completes. This would leave orders unmanaged at their initial price for 120+ seconds until the timeout sweep cancels them.

---

### 4. **RiskEngine** (`src/risk/risk_engine.py`)

**Purpose:** Enforce position size limits before orders are submitted.

**Risk Checks (In Order):**

```python
def compute_allowed_qty(signal):
    # 1. Max contracts per trade
    qty = min(signal.qty, self.max_contracts)
    
    # 2. Max daily premium (new in this session)
    if self.max_premium_per_trade:
        per_contract_premium = abs(signal.limit_price) * 100.0
        max_by_premium = int(self.max_premium_per_trade / per_contract_premium)
        qty = min(qty, max_by_premium)
    
    # 3. Position sizing from strategy config
    # (e.g., risk 1% of portfolio per position)
    portfolio_value = self.portfolio_size
    risk_pct = strategy.position_sizing_pct
    max_by_portfolio = int((portfolio_value * risk_pct) / signal.limit_price)
    qty = min(qty, max_by_portfolio)
    
    return qty
```

**Example:**
```yaml
# configs/risk.yaml
global:
    max_contracts: 10              # Max 10 contracts per order
    max_premium_per_trade: 2000.0  # Max $2,000 premium per trade
    
xsp_short_straddle:
    max_contracts: 10
    position_sizing_pct: 0.025     # Risk 2.5% of portfolio
```

With this config, if straddle call costs $2.00:
- Per-contract premium = $2.00 * 100 = $200
- Max qty by premium = $2000 / $200 = 10 contracts ✓ (within max_contracts)

If instead call costs $10.00:
- Per-contract premium = $10.00 * 100 = $1,000
- Max qty by premium = $2000 / $1,000 = 2 contracts (clamped)

---

### 5. **PositionManager & PositionStore** (`src/portfolio/position_manager.py`, `src/storage/position_store.py`)

**PositionManager (In-Memory):**
- Tracks open positions with live PnL updates
- Maps order fills to position ownership
- Manages position lifecycle (OPENING → OPEN → CLOSED)

**PositionStore (SQLite Persistence):**
- Persists position-to-strategy attribution across restarts
- Stores realized PnL and close reason (SL, time, manual)
- Supports multi-day PnL bucketing

**Why Persistence?** 
On 2026-06-10, a position was re-seeded from the broker after restart, but its originating strategy was guessed by underlying symbol (wrong strategy). This caused the wrong exit rules to apply. Now the position_attribution table stores the true strategy_id.

**Schema:**
```sql
CREATE TABLE position_attribution (
    position_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,          -- e.g., "XSP"
    expiry TEXT NOT NULL,          -- e.g., "20260612"
    strike REAL NOT NULL,          -- e.g., 740.0
    right TEXT NOT NULL,           -- "CALL" or "PUT"
    side TEXT NOT NULL,            -- "BUY" or "SELL"
    quantity INTEGER NOT NULL,     -- signed: +5 long, -5 short
    avg_entry_price REAL NOT NULL,
    entry_time TEXT,               -- ISO format
    status TEXT NOT NULL,          -- "OPEN", "OPENING", "CLOSED"
    realized_pnl REAL,             -- Final PnL
    close_reason TEXT,             -- "STOP_LOSS", "TIME_EXIT", "MANUAL"
    closed_at TEXT                 -- ISO format when closed
);
```

**Daily PnL Query:**
```python
def daily_pnl_summary(self, days=30):
    """Realized PnL grouped by America/New_York close date."""
    by_day = {}
    for pos in closed_positions:
        ny_date = pos.closed_at.astimezone(ny_tz).date()
        by_day[ny_date]['realized_pnl'] += pos.realized_pnl
        by_day[ny_date]['closed_positions'] += 1
    return by_day
```

Returns:
```json
{
  "today": {
    "date": "2026-06-12",
    "realized_pnl": 450.50,
    "closed_positions": 3,
    "per_strategy": {
      "xsp_straddle_1100_40": 250.00,
      "xsp_breakout_0945": 200.50
    }
  },
  "days": [
    {"date": "2026-06-12", "realized_pnl": 450.50, "closed_positions": 3},
    {"date": "2026-06-11", "realized_pnl": -125.00, "closed_positions": 2}
  ]
}
```

---

### 6. **ExitManager** (`src/portfolio/exit_manager.py`)

**Purpose:** Manages stop-loss and time-based exits.

**Stop-Loss Logic:**

```python
def check_stop_loss(position):
    # Get current quote
    quote = broker.get_quote(position.contract)
    
    # Only check if quote is fresh (prevents stale-data stops)
    if quote.staleness > MAX_QUOTE_AGE:
        return False
    
    # Calculate current mark
    mark = (quote.bid + quote.ask) / 2.0
    
    # Compute P&L
    entry_premium = position.avg_entry_price * 100.0
    current_premium = mark * 100.0
    pnl = position.side_multiplier * (entry_premium - current_premium)
    
    # Check stop
    stop_loss_threshold = position.stop_loss_pct * entry_premium
    if pnl <= -stop_loss_threshold:
        return True
    
    return False
```

**Time Exit Logic:**

```python
def check_time_exit(position):
    # Position has exit_time_utc set by strategy config
    return now(UTC) >= position.exit_time_utc
```

---

### 7. **Dashboard** (`src/dashboard/app.py` + `index.html`)

**Architecture:**

```
Browser (index.html)
    ↓
FastAPI (port 8500)
    ├─ GET /                   → Serve UI (cached: no-cache header)
    ├─ GET /api/state          → Runtime snapshot (positions, orders, PnL)
    ├─ GET /api/events         → Recent events (read-only)
    ├─ GET /api/commands       → Recent commands executed
    ├─ GET /api/pnl            → Daily + all-time PnL breakdown
    ├─ GET /api/history        → Closed positions
    ├─ GET /api/logs           → Tail of runner.log
    ├─ POST /api/commands      → Enqueue manual command
    └─ WS /ws                  → Live state push (1s cadence)
```

**Command Queue (Manual Control):**

User clicks "Close Position" on dashboard → POST to `/api/commands`:
```json
{
  "type": "close_position",
  "payload": {
    "position_id": "uuid-123",
    "reason": "MANUAL"
  }
}
```

This inserts a row into the `commands` table (SQLite). The runner drains this queue each tick and executes commands.

**PnL API Response:**

```json
{
  "strategies": {
    "xsp_straddle_1100_40": {
      "realized_all_time": 1250.50,
      "realized_today": 450.50,
      "unrealized_live": -75.25,
      "total": 1175.25,
      "closed_positions": 8
    }
  },
  "total_pnl": 1175.25,
  "today": {
    "date": "2026-06-12",
    "realized": 450.50,
    "unrealized": -75.25,
    "total": 375.25,
    "closed_positions": 3,
    "per_strategy": {...}
  },
  "daily": [
    {"date": "2026-06-12", "realized_pnl": 450.50, "closed_positions": 3},
    {"date": "2026-06-11", "realized_pnl": -125.00, "closed_positions": 2}
  ]
}
```

**UI Tabs:**

| Tab | Purpose | Data Source |
|-----|---------|-------------|
| Live | Active positions, orders, compact PnL strip | /api/state, WebSocket |
| PnL | Daily breakdown, per-strategy performance | /api/pnl |
| Orders | Order history with fill details | /api/events |
| Logs | Real-time runner logs | /api/logs |

---

## Data Flow

### Entry → Fill → Exit Flow

```
1. ENTRY SIGNAL
   Strategy.poll() → signal = (side=BUY, qty=5, limit=2.50)
   
2. RISK CHECK
   RiskEngine.compute_allowed_qty(signal) → qty=5 (passes all checks)
   
3. ORDER SUBMISSION
   OrderManager.submit_order(contract, side, qty, limit_price)
   → IBKRBroker.submit_order() → order_id assigned
   → order state = SUBMITTED
   
4. WORKING
   IBKR assigns working order
   → OrderManager.on_order_status(order_id, state=WORKING)
   → Start repricing task (2s cadence, strong ref held)
   
5. REPRICING (Continuous)
   OrderManager._reprice_order_loop():
   - Every 2s: check quote staleness
   - If quote fresh: compute new price based on bid/ask
   - If price changed: IBKRBroker.modify_order(new_price)
   
6. FILL
   IBKRBroker.on_fill(order_id, fill_price, fill_qty)
   → OrderManager.process_fill(order_id, fill_price, fill_qty)
   → PortfolioManager.create_position(contract, side, qty, entry_price)
   → PositionStore.upsert_position() [async, with retry on DB lock]
   → EventStore.log_fill_event()
   → order state = FILLED
   
7. STOP-LOSS MONITORING
   ExitManager._check_exits():
   - Every tick: compare current quote to stop_loss_pct threshold
   - If breached: submit exit order
   
8. EXIT ORDER
   Same flow as entry: SUBMITTED → WORKING → REPRICED → FILLED
   
9. CLOSE & REALIZE PnL
   PositionManager.close_position(position_id, exit_price)
   → Compute realized_pnl = (entry_price - exit_price) * qty * 100
   → PositionStore.upsert_position(status=CLOSED, realized_pnl, close_reason)
   → EventStore.log_close_event()
   → Position now appears in /api/history
```

### Manual Control Flow

```
Dashboard (Browser)
    ↓
POST /api/commands
    ↓
CommandQueue.enqueue("close_position", payload)
    ↓
SQLite: INSERT INTO commands TABLE
    ↓
Runner._tick() drains commands table
    ↓
Runner._execute_command(command)
    ↓
ExitManager._submit_exit(position, reason="MANUAL")
    ↓
OrderManager → IBKRBroker → IBKR TWS
    ↓
Fill → PositionStore.upsert_position(close_reason="MANUAL")
    ↓
Dashboard shows in history with reason
```

---

## Module Reference

### `src/strategies/` — Entry Signal Generators

Each strategy implements `StrategyProvider` interface:

```python
class XSPShortStraddle(StrategyProvider):
    def poll(self, state: PortfolioState) -> Optional[Signal]:
        """Generate entry signal if conditions met."""
        
        # Only trade if not already in position
        if self.position_id in state.open_positions:
            return None
        
        # Check entry time window
        now = datetime.now(UTC)
        if not (self.entry_time <= now.time() <= self.entry_time_end):
            return None
        
        # Check trade-once-per-day gate
        traded_today = self._trader_today in state.traded_today
        if traded_today and not allow_reentry:
            return None
        
        # Get option chain
        chain = self.broker.get_chain(symbol="XSP", expiry="20260612")
        
        # Find strike closest to current spot
        spot = chain.spot_price
        atm_strike = chain.find_nearest_strike(spot)
        
        # Get quotes for ATM call and put
        call = chain.get_option("CALL", atm_strike)
        put = chain.get_option("PUT", atm_strike)
        call_quote = self.broker.get_quote(call)
        put_quote = self.broker.get_quote(put)
        
        # Return multileg signal
        return Signal(
            legs=[
                Leg(contract=call, side=SELL, qty=qty),
                Leg(contract=put, side=SELL, qty=qty),
            ],
            limit_price=call_quote.ask + put_quote.ask,
            reason="ATM_STRADDLE"
        )
    
    def create_orders(self, qty, limit_price):
        """Generate individual order objects from signal."""
        # Return list of orders with limit prices
```

**Config:**
```yaml
xsp_straddle_1100_40:
    enabled: true
    entry.entry_time: "11:00"         # Start trading at 11 AM ET
    entry.entry_time_end: "11:05"     # Stop at 11:05 AM ET
    entry.max_contracts: 10           # Max order size
    exit.stop_loss_pct: 40.0          # Exit if down 40% of entry premium
    exit.time_exit_utc: "15:30"       # Close by 3:30 PM ET
    position_sizing_pct: 0.025        # Risk 2.5% of portfolio
    leverage: 12.0                    # Account leverage multiplier
    allow_reentry: true               # Can trade again same day
```

### `src/core/models.py` — Data Models

```python
# Order
@dataclass
class Order:
    order_id: UUID
    contract: Contract
    side: Side  # BUY or SELL
    qty: int
    limit_price: float
    state: OrderState  # SUBMITTED, WORKING, FILLED, etc.
    submitted_at: datetime
    time_to_live: timedelta  # 60s default
    expiry_time: datetime
    fill_price: Optional[float]
    fill_qty: int = 0

# Position
@dataclass
class Position:
    position_id: UUID
    strategy_id: str
    contract: Contract
    side: Side  # BUY (long) or SELL (short)
    quantity: int
    average_entry_price: float
    entry_time: datetime
    status: PositionStatus  # OPENING, OPEN, CLOSED
    stop_loss_pct: float  # e.g., 40.0 = 40%
    exit_time_utc: datetime
    realized_pnl: Optional[float]
    close_reason: Optional[str]  # "STOP_LOSS", "TIME_EXIT", "MANUAL"

# Event (immutable)
@dataclass
class Event:
    event_id: UUID
    timestamp: datetime
    event_type: str  # "FILL", "CANCEL", "REJECT", "CLOSE", etc.
    strategy_id: str
    order_id: Optional[UUID]
    position_id: Optional[UUID]
    payload: dict  # Event-specific data
```

---

## Task Coordination

### Async Task Management

The system uses asyncio for concurrency with **strong reference management** to prevent GC.

**Background Tasks (Runner):**
- Repricer loops (one per working order)
- Cancel confirmation waits
- Stop-loss monitoring
- Time-exit monitoring
- Broker reconnect

**Problem Before Fix:** Bare `asyncio.create_task()` → task garbage collected before completion → orders unmanaged for 120+ seconds.

**Solution:** Hold strong references via `_background_tasks` set with cleanup callbacks:

```python
class OrderManager:
    def __init__(self):
        self._background_tasks: set[asyncio.Task] = set()
    
    def _spawn(self, coro):
        """Spawn task with strong reference."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task
    
    def start_repricing(self, order_id):
        # ✓ Correct: task held in set
        self._spawn(self._reprice_order_loop(order_id))
        
        # ❌ Wrong: task GC'd after function returns
        # asyncio.create_task(self._reprice_order_loop(order_id))
```

### Event Loop Blocking

**Principle:** DB-touching endpoints run on threadpool (not `async def`) to avoid blocking event loop under contention.

```python
# ✓ Correct: FastAPI runs on threadpool
@app.get("/api/state")
def get_state():
    store, _ = _stores()
    return store.read()  # Blocking SQLite read, runs on thread

# ❌ Wrong: blocks event loop
@app.get("/api/state")
async def get_state():
    store, _ = _stores()
    return store.read()  # Blocks event loop waiting for DB
```

---

## Error Handling & Recovery

### Reconciliation (Fail-Closed)

```python
def _tick(self):
    # ... normal tick logic ...
    
    if not self._reconcile():
        logger.critical("Reconciliation failed")
        self.override_manager.state.system_locked = True
        return  # Stop accepting new trades
    
    # System is locked. Operator must:
    # 1. Check dashboard for mismatch details
    # 2. Manually sync state via MANUAL CONTROL commands
    # 3. Clear lock once resolved
```

**Dashboard Control:** Manual "Unlock System" button calls `CommandQueue.enqueue("unlock_system")`.

### Order Timeout & Cleanup

```python
def _sweep_expired_orders(self):
    """Cancel orders that exceeded time_to_live."""
    now = datetime.now(UTC)
    for order in self.order_manager.working_orders():
        if now >= order.expiry_time:
            logger.warning(f"Order {order.id} expired, cancelling")
            self._spawn(self.broker.cancel_order(order.id))
            order.state = OrderState.TIMEOUT
```

### Shutdown Safety

```python
async def _shutdown(self):
    """Clean exit: cancel working orders before disconnect."""
    logger.info("Shutting down...")
    
    # Cancel all working orders
    working = self.order_manager.working_orders()
    for order in working:
        try:
            await self.broker.cancel_order(order.id)
        except Exception as e:
            logger.error(f"Failed to cancel {order.id}: {e}")
    
    # Wait up to 5s for cancel ACKs
    timeout = datetime.now(UTC) + timedelta(seconds=5)
    while self.order_manager.working_orders() and datetime.now(UTC) < timeout:
        await asyncio.sleep(0.1)
    
    # Force-cancel any stragglers
    for order in self.order_manager.working_orders():
        order.state = OrderState.FORCE_CANCELLED
    
    # Disconnect
    await self.broker.disconnect()
```

### Exit Retry Backoff

```python
def _submit_exit(self, position):
    try:
        order = self._create_exit_order(position)
        self.broker.submit_order(order)
        self._exit_attempts[position.id] = (0, now)
    except RejectionError as e:
        if "Cannot have open orders on both sides" in str(e):
            attempts, first_fail_time = self._exit_attempts.get(position.id, (0, now))
            
            if attempts == 0:
                # First failure: retry after 10s
                self._schedule_retry(position, delay=10)
            elif attempts >= 5:
                # Many failures: operator alert + slow retry
                operator_alert(f"Exit stuck on {position.contract} ({attempts} attempts)")
                self._schedule_retry(position, delay=60)
            else:
                # Middle ground: standard retry
                self._schedule_retry(position, delay=10)
            
            self._exit_attempts[position.id] = (attempts + 1, first_fail_time)
```

---

## Summary

This system balances **aggressive execution** (2s repricing, in-place modifies) with **robustness** (reconciliation locks, strong task refs, shutdown cleanup). The database-centric design allows operator intervention without stopping the runner, and comprehensive logging enables post-mortem analysis of any issues.
