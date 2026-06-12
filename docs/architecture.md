# Architecture

Last updated: 2026-06-11 (post live-shakeout revamp).

## Overview

Intraday options execution system for IBKR (paper). A single asyncio
process (`run_paper_trading.py` → `ExecutionRunner`) owns the broker
connection; a separate FastAPI dashboard process is a pure SQLite
reader/writer with **zero broker access** (enforced by an AST-level
test). AGENTS.md hard rules govern everything.

## Component Diagram

```
                       ┌────────────────────────────── runner process ─┐
 strategies.yaml ──►   │  ExecutionRunner (tick loop, 1s)              │
 risk.yaml       ──►   │   ├─ CompositeStrategyProvider ── poll()      │
 overrides.yaml  ──►   │   │    ├─ XSPBreakout / XSPBreakoutLate       │
 strategy_       ──►   │   │    ├─ XSPShortStraddle                    │
   overrides.yaml      │   │    └─ DummyTest (shakeout only)           │
                       │   ├─ RiskEngine (every entry)                 │
                       │   ├─ OrderManager ── repricer (cancel/replace,│
                       │   │     replacement chains, orderRef, algo)   │
                       │   ├─ ExitManager (hybrid SL: 1-min bar trigger│
                       │   │     + tick-mid guard; time exits; force)  │
                       │   ├─ PositionManager (in-memory book)         │
                       │   ├─ ReconciliationEngine (startup + 60s)     │
                       │   └─ IBKRBrokerClient (ib_async: streaming    │
                       │        quotes, 1-min BarBuilder, Adaptive algo│
                       └───────────────┬───────────────────────────────┘
                                       │ SQLite (data/events.db, WAL)
        events / runtime_state / commands / position_attribution
                                       │
                       ┌───────────────┴───────────────┐
                       │  FastAPI dashboard (DB-only)   │
                       │  src/dashboard/app.py + static │
                       └────────────────────────────────┘
```

## Signal Flow

signal → RiskEngine → `OrderManager.submit_intent` (builds orderRef
`{strategy}:{position_id|new}:{leg}:{side}:{unix_ms}`; attaches IBKR
Adaptive algo during RTH) → limit order → repricer chases the touch
every 2s via cancel/replace. Each replacement is linked
`old.superseded_by = new` (the replacement chain).

Fills → PositionManager → exit rules applied from the strategy's config
(stop %, time exit NY) → attribution persisted to SQLite.

## Multi-Leg Coordination (replacement chains)

A leg has FAILED only when the FINAL order of its replacement chain is
terminal (CANCELLED/REJECTED/ERROR) with ZERO fill. Routine repricer
cancels are *superseded*, never failures. True failure → hard-cancel
working peer legs + flatten filled peers (asymmetric protection). A
position's own chain never flattens it (partial fill + cancelled
remainder is a valid position).

History: treating routine cancels as failures nuked straddle legs on
every reprice cycle (live incidents 2026-06-11). See
`tests/integration/test_multileg_coordination.py`.

## Event Loop Model

One asyncio loop. The tick (default 1s) runs: connectivity check →
command-queue drain → active-order management (timeouts, multi-leg
coordination) → strategy polling (entries) → exit checks → runtime
snapshot publish. Reconciliation every 60s. The repricer runs as a task
per working order.

## Error Handling Philosophy / Fail-Closed Behavior: PROTECT MODE

A lock (broker disconnect, reconciliation mismatch, broker reject,
operator action) blocks NEW ENTRIES ONLY. The tick loop keeps running:

- exit management (stops, time exits, force flatten) continues
- dashboard commands continue (exit/flatten/cancel/**unlock**)
- disconnect-provenance locks auto-resume after a clean reconciliation
- the runtime snapshot keeps publishing (dashboard stays truthful)

Open positions are **never abandoned**. Unlock from the dashboard is
reconciliation-gated: it succeeds only into a verified-clean state.
`scripts/run_supervised.sh` relaunches the runner after crashes or a
dashboard-initiated restart; dashboard Shutdown ends it for good.

## Storage Layer (data/events.db, WAL + busy_timeout everywhere)

| Table | Purpose |
|---|---|
| events | append-only audit log (indexed: timestamp, type+timestamp) |
| runtime_state | single-row live snapshot for the dashboard |
| commands | dashboard → runner control queue (durable) |
| position_attribution | position→strategy + realized_pnl + close_reason + closed_at; restart seeding reads this |

Plus `data/bars/bars_YYYYMMDD.jsonl` (1-min bars, NY-aligned, for the
shadow-replay acceptance test) and `reports/shadow_*.md`.

## Configuration Loading

Startup-only (`_load_configs`): risk.yaml, strategies.yaml,
overrides.yaml, then `strategy_overrides.yaml` (dashboard-made parameter
edits) layered on top. Dashboard `update_strategy` commands apply live
in-memory AND persist to the overlay file. Any base-config change still
requires a runner restart (use the dashboard Restart button).

## Component Boundaries

1. Strategy code never touches the broker for orders — signals only.
2. Every entry passes RiskEngine; exits use permissive decisions.
3. Limit orders only (the Adaptive algo wraps a limit; MKT exists only
   for emergency flatten).
4. Exit limits are touch-priced and NBBO-clamped by construction
   (IBKR Error 202 impossible).
5. One process owns the broker; the dashboard physically cannot reach it.
6. Every order carries an orderRef audit tag visible in TWS/Flex.
