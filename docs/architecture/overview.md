# Architecture Overview

The system is a single-runner, database-backed execution system for
intraday options strategies.

## Processes

```text
TWS / IB Gateway
      ^
      |
run_paper_trading.py
  ExecutionRunner
  - strategy polling
  - risk checks
  - order submission
  - fill handling
  - exits
  - reconciliation
      |
      v
SQLite data/events.db
  - events
  - runtime_state
  - commands
  - position attribution
      ^
      |
FastAPI dashboard
  src/dashboard/app.py
  - reads state
  - enqueues commands
  - never imports or calls the broker
```

## Execution Loop

The runner owns the broker connection and runs on a single asyncio event
loop. Each tick performs the same high-level work:

1. Check broker connectivity.
2. Drain pending dashboard commands.
3. Manage active orders and stuck cancellations.
4. Poll strategies for entry signals when entries are allowed.
5. Run exit management for open positions.
6. Publish runtime state to SQLite.
7. Reconcile broker and internal state on the configured interval.

## Signal Flow

Strategies emit `StrategySignal` objects. The runner selects contracts,
passes entries through `RiskEngine`, then submits approved limit orders
through `OrderManager`.

Fills update `PositionManager`. `ExitManager` owns stop, take-profit,
time-exit, strategy-exit, and forced-exit handling after a position is
opened.

## Locked Mode

Locked mode blocks new entries. It does not freeze the system.

While locked, the runner still processes:

- exit management
- flatten commands
- cancel commands
- dashboard state publishing
- reconciliation
- unlock attempts gated by reconciliation

Disconnect-provenance locks may auto-clear after reconnect plus clean
reconciliation. Manual and mismatch locks require operator review.

## Boundaries

- The dashboard is database-only and does not call the broker.
- Orders pass through the runner execution path.
- Entry orders pass through `RiskEngine`.
- Normal orders are limit orders.
- Market orders are reserved for explicit emergency flatten behavior.
- Internal accounting uses position IDs and strategy IDs, not only broker
  account summaries.

Implementation note: some current strategy providers receive the broker
client for market-data access. They still must not submit, cancel, or
manage orders.

