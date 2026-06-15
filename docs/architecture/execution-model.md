# Execution Model

This page summarizes the risk, order, and position lifecycle in the
runner.

## Entry Path

1. Strategy emits a `StrategySignal`.
2. Runner selects or qualifies the contract.
3. `RiskEngine` approves, reduces, or rejects the entry.
4. `OrderManager` creates and submits a limit order.
5. Broker callbacks update order state.
6. Fill events update the position book.

## Risk Decision

`RiskEngine` is the entry gate. It evaluates the signal against current
system state, configuration, quote quality, position limits, and manual
overrides.

The output is a `RiskDecision`:

- `approved`
- `allowed_quantity`
- `blocking_reasons`
- `warnings`
- `risk_decision_id`

`allowed_quantity` is authoritative. Risk may reduce requested quantity,
but it must not increase it.

Required checks include strategy enablement, trading mode, manual
overrides, symbol enablement, market hours, loss limits, position limits,
open-order limits, duplicate exposure, buying power estimate, quote
freshness, bid/ask validity, spread, contract qualification, kill switch,
and cooldown state.

Rejected entries must not produce orders.

## Order States

```text
NEW
  -> RISK_CHECKED
  -> SUBMITTED
      -> PARTIALLY_FILLED
      -> FILLED
      -> CANCEL_PENDING
          -> CANCELLED
      -> REJECTED
      -> ERROR
```

Broker callbacks update the internal order book and are logged to the
event store.

If an order is replaced, replacement-chain tracking must distinguish a
routine superseded cancel from a real terminal failure.

## Position States

```text
OPENING
  -> OPEN
  -> PARTIALLY_CLOSED
  -> CLOSED
  -> FORCE_CLOSED
```

Positions are tracked internally with `position_id` and `strategy_id`.
Broker account summaries are used for reconciliation, not as the only
source of internal accounting.

## Partial Fills

- Partial entry fill: cancel remainder, manage the filled quantity.
- Partial exit fill: leave the residual position open and continue exit
  management.

Strategy configuration may override defaults where explicitly supported.

## Exit Ownership

After a position is opened, `ExitManager` owns exit handling:

- stop price
- take-profit price
- time exit
- strategy-requested exit
- forced flatten

If a signal omits optional exit fields, defaults come from the strategy
configuration.

## Fail-Closed Events

Rejected orders, unknown order state, unknown position state, broker
disconnects, stale quotes, and reconciliation mismatches are safety
events. The system should block new entries and preserve operator exit
paths.

