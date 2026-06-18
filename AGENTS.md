# AGENTS.md — Intraday Options Execution System

## What this system is

A modular, production-grade intraday options execution system for
Interactive Brokers. It executes pre-built systematic strategies.
It does not invent signals or trading rules.

This document is authoritative. If any agent is uncertain about a
design decision, it must check here first and document an assumption
in docs/assumptions.md if the answer is not here.

---

## Hard rules — these override everything

1. Strategy code never calls the broker directly. Ever.
2. Strategy code never manages orders, fills, or broker state.
3. Strategy code only emits StrategySignal objects.
4. Every order passes through RiskEngine before submission.
5. Normal entries and exits use limit orders only.
6. Market orders are disabled except explicit emergency flatten mode.
7. The system fails closed on: stale quotes, missing quotes, broker
   disconnect, rejected orders, unknown order state, unknown position
   state, reconciliation mismatch.
8. MockBrokerClient is implemented and tested before IBKRBrokerClient.
9. IBKR paper trading is implemented and tested before live trading.
10. Every signal, risk decision, order event, fill event, position
    update, exit decision, manual override, reconciliation event,
    broker callback, and error must be logged to EventStore.
11. Positions are tracked internally using position_id + strategy_id.
    Never rely only on broker position summary for internal accounting.
12. On startup, reconcile internal state with broker state before
    allowing new trades.
13. If broker/internal state does not match, enter reduce-only or
    locked mode immediately.
14. If uncertain about broker behavior or market-data behavior,
    document the assumption in docs/assumptions.md and fail closed.
15. No Kafka, Redis, Docker Compose, microservices, or custom frontend
    unless explicitly requested.

---

## Live trading safety — all gates must pass

Live trading requires ALL of the following simultaneously:
- `live_trading.enabled: true` in configs/broker.yaml
- Environment variable `ALLOW_LIVE_TRADING=I_UNDERSTAND_THIS_CAN_LOSE_MONEY`
- Account ID present in account allowlist
- Paper-mode tests passing
- Reconciliation implemented and tested
- Kill switches implemented and tested
- Manual flatten and cancel controls implemented and tested

Live trading is DISABLED by default. Any agent that enables it
without all gates passing has violated the spec.

---

## Tech stack — pinned

- Python 3.11+
- pydantic >= 2.0 (v2 APIs only, no v1 compat shims)
- ib_async >= 1.0
- SQLite (default) or DuckDB for local state and event logs
- YAML for all config
- pytest for all tests
- asyncio event loop, async-first throughout

---

## Async model

The system runs on a single asyncio event loop. All components are
async-first. Blocking calls are not permitted in the hot path.
ib_async callbacks integrate directly into this loop.

---

## Signal dispatch

The runner uses a poll loop inside the asyncio event loop. Each tick:
1. Poll each active strategy.
2. If a StrategySignal is returned, pass it directly through the
   execution pipeline as a function call chain.
3. Write events to EventStore at each step.

No internal pub/sub, no message queue, no threads in v1.

---

## Quote staleness contract

- A quote is stale if its timestamp age exceeds max_age_seconds,
  defined per symbol type in configs/symbols.yaml.
- QuoteCache stores only the latest snapshot per symbol.
- Any data gap during broker disconnect resets freshness to stale
  until a new tick arrives.
- No sequence-number tracking in v1.
- The system rejects or pauses on stale quotes, never trades through.

---

## Position sizing contract

- StrategySignal includes a requested_quantity field.
- RiskEngine may reduce it, never increase it.
- The approved allowed_quantity in RiskDecision is what OrderManager
  uses. This is authoritative.
- Strategies never specify dollar sizing directly.

---

## Exit rule ownership

- StrategySignal may include optional: stop_price, take_profit_price,
  time_exit_utc.
- If absent, ExitManager uses defaults from strategy YAML config.
- The strategy never modifies exit rules post-entry.
- ExitManager owns all exit state after position creation.

---

## Partial fill defaults

- Partial entry fill: cancel remainder, manage position at filled
  quantity, resize stop/target proportionally.
- Partial exit fill: leave remainder open, continue managing residual.
- Strategy config may override these defaults.

---

## Component boundaries

| Component            | Can call                                      | Cannot call         |
|----------------------|-----------------------------------------------|----------------------|
| Strategy             | nothing (emits signal only)                   | everything else      |
| RiskEngine           | QuoteCache, config, EventStore                | BrokerClient         |
| OrderManager         | BrokerClient, EventStore                      | Strategy, RiskEngine |
| PositionManager      | EventStore                                    | BrokerClient         |
| ExitManager          | PositionManager, EventStore                   | BrokerClient         |
| ManualControlService | OrderManager, PositionManager, EventStore     | BrokerClient directly|
| Dashboard/CLI        | ManualControlService only                     | BrokerClient         |
| ReconciliationEngine | BrokerClient (read only), PositionManager     | OrderManager         |

---

## Core models — required typed objects

Build Pydantic v2 models for all of the following:

StrategySignal, OptionContract, QuoteSnapshot, AccountState,
RiskConfig, RiskDecision, OrderIntent, OrderPlan, OrderState,
OrderEvent, FillEvent, Position, ExitRule, ManualOverride,
ExecutionReport, ReconciliationReport

---

## Order lifecycle states

NEW → RISK_CHECKED → SUBMITTED → PARTIALLY_FILLED → FILLED
                                → CANCEL_PENDING → CANCELLED
                                → REJECTED
                                → ERROR

---

## Position lifecycle states

OPENING → OPEN → PARTIALLY_CLOSED → CLOSED → FORCE_CLOSED

---

## RiskEngine checks (exhaustive)

strategy enabled, global trading mode, manual overrides,
symbol disabled/enabled, market open, no-new-trades cutoff,
daily loss limit, per-strategy daily loss limit,
max contracts per trade, max premium per trade,
max open positions, max positions per strategy,
max positions per underlying, max open orders,
duplicate exposure, buying power estimate,
quote freshness, bid/ask validity, max spread percentage,
contract qualification, kill-switch state, cooldown state

RiskEngine returns: approved, allowed_quantity, blocking_reasons,
warnings, risk_decision_id

---

## Manual control commands

All manual controls route through ManualControlService.
ManualControlService never calls BrokerClient directly.
All manual actions are logged to EventStore.

Required commands:
status, pause-strategy, resume-strategy, disable-symbol,
enable-symbol, reduce-only, flatten-position, flatten-strategy,
flatten-all, cancel-order, cancel-all, lock-system,
show-risk, show-positions, show-orders, show-rejections

---

## Phase plan

| Phase | Scope                                        | Status    |
|-------|----------------------------------------------|-----------|
| 0     | Repo structure, configs, docs stubs          | complete  |
| 1     | Core models and validation tests             | complete  |
| 2     | Config loading and validation                | complete  |
| 3     | RiskEngine (deterministic, no broker)        | complete  |
| 4     | MockBrokerClient                             | complete  |
| 5     | OrderManager                                 | complete  |
| 6     | PositionManager and ExitManager              | complete  |
| 7     | OptionContractSelector                       | complete  |
| 8     | EventStore and audit logs                    | complete  |
| 9     | ManualControlService and CLI                 | complete  |
| 10    | ReconciliationEngine                         | complete  |
| 11    | IBKR setup docs and diagnostic scripts       | complete  |
| 12    | IBKRBrokerClient, paper mode                 | complete  |
| 13    | Execution analytics                          | complete  |
| 14    | FastAPI dashboard                            | complete  |
| 15    | Paper trading runner                         | complete  |
| 16    | Live mode safety gates                       | complete  |


Agents must not work ahead of the current phase.
Agents must update the Status column when a phase is complete.

---

## Test requirements

For every component, tests must cover:
- At least one happy path
- At least one rejection/failure path
- At least one edge case (partial fill, stale quote, mismatch, etc.)

Do not write tests that only verify an object can be constructed.
Tests go in tests/unit/ or tests/integration/ as appropriate.

---

## Repo structure

execution-system/
  AGENTS.md
  README.md
  pyproject.toml
  .env.example

  configs/
    strategies.yaml
    risk.yaml
    symbols.yaml
    broker.yaml
    overrides.yaml
    dashboard.yaml

  docs/
    architecture.md
    assumptions.md
    risk_spec.md
    order_lifecycle.md
    position_lifecycle.md
    manual_controls.md
    ibkr_setup.md              ← Phase 11
    market_data_requirements.md ← Phase 11
    paper_trading_checklist.md ← Phase 15
    live_trading_checklist.md  ← Phase 16

  scripts/
    check_ibkr_connection.py   ← Phase 11
    check_market_data.py       ← Phase 11
    check_option_chain.py      ← Phase 11

  src/
    app/
      runner.py
      control.py
      status.py
    control/
      manual_control.py
      overrides.py
    core/
      models.py
      enums.py
      config.py
      clock.py
      events.py
      errors.py
    strategies/
      base.py
      example_strategy.py
    marketdata/
      quote_cache.py
      option_chain.py
      contract_selector.py
      data_quality.py
    risk/
      risk_engine.py
      limits.py
      kill_switch.py
    execution/
      execution_planner.py
      order_manager.py
      repricer.py
      fill_handler.py
    portfolio/
      position_manager.py
      exit_manager.py
      pnl.py
      reconciliation.py
    broker/
      interface.py
      mock_broker.py
      ibkr_broker.py          ← Phase 12
    storage/
      db.py
      event_log.py
      repositories.py
    analytics/
      execution_quality.py
      reports.py
    dashboard/                 ← Phase 14
      app.py
      static/index.html

  tests/
    unit/
      test_models.py
      test_config.py
      test_risk_engine.py
      test_contract_selector.py
      test_order_manager.py
      test_position_manager.py
      test_exit_manager.py
      test_reconciliation.py
      test_manual_control.py
      test_execution_quality.py
    integration/
      test_mock_execution_flow.py
      test_ibkr_paper_connection.py  ← Phase 12

---

## What agents must never do

- Call BrokerClient from strategy code
- Enable live trading without all safety gates
- Skip RiskEngine for any order
- Use pydantic v1 APIs
- Add infrastructure (Redis, Kafka, Docker, cloud) without explicit request
- Work ahead of the current phase
- Guess at IBKR behavior; document assumptions instead
- Write tests that only test construction
