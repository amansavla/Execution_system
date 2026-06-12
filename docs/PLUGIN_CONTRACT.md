# Strategy Plugin Contract

How to add a new strategy WITHOUT touching core execution code
(runner, order manager, exit manager, risk engine, brokers).

Verified in Phase 7: two strategies (breakout + short straddle) run
simultaneously on the same underlying with correct attribution under
normal operation, after broker reconnect, and after process restart
(`tests/integration/test_multi_strategy.py`).

## What a strategy IS

A strategy is a `StrategyProvider` subclass whose only job is to emit
`StrategySignal` objects from `poll()`. Per AGENTS.md hard rules 1–3 it
**never** calls the broker for orders, never manages orders/fills/
positions, and never mutates execution state.

```python
from src.app.runner import StrategyProvider

class MyStrategyProvider(StrategyProvider):
    def __init__(self, broker, position_manager=None): ...
    def set_position_manager(self, pm): ...          # optional, for dedup
    async def poll(self, strategy_config, current_time) -> list[StrategySignal]:
        ...
```

`poll()` is called once per runner tick for every enabled config whose
`entry.signal_source` maps to this provider. It MAY read market data via
`self.broker.get_quotes(...)` / `self.broker.get_latest_completed_bar(...)`
(read-only broker access is allowed; order routing is not).

## The three integration points (the ONLY things you touch)

1. **Provider module** — `src/strategies/my_strategy.py` implementing
   the class above. Signal triggers should evaluate on 1-min bar closes
   (`broker.get_latest_completed_bar(symbol)`) to match backtest
   semantics; see `xsp_breakout.py` for the pattern.

2. **Registration** — one line in `run_paper_trading.py`'s
   `CompositeStrategyProvider` map:
   ```python
   strategy_provider = CompositeStrategyProvider({
       "xsp_breakout":        breakout_provider,
       "xsp_short_straddle":  straddle_provider,
       "my_signal_source":    MyStrategyProvider(broker=broker_client),
   })
   ```
   Dispatch is by `entry.signal_source` (`src/strategies/composite.py`).

3. **Config block** — `configs/strategies.yaml` entry:
   ```yaml
   - strategy_id: "my_strategy_1000"
     enabled: true
     underlying: "XSP"
     option_type: "single"
     direction: "long"            # long | short
     dte_target: 0
     entry:
       signal_source: "my_signal_source"   # -> provider map key
       max_contracts: 5                    # safety cap on sizing
       limit_price_offset: 0.05
       order_timeout_seconds: 90
     exit:
       stop_loss_pct: 30          # % of entry premium
       time_exit_utc: "15:20"     # NOTE: interpreted as NY local time
       use_mid_for_exits: false
     position_sizing_pct: 0.01    # optional, % of NetLiq
     leverage: 12.0               # optional (margin-based sizing)
   ```

## What the platform does for you (do NOT reimplement)

- **Risk**: every signal passes RiskEngine (cutoffs, limits, spread,
  freshness) before any order exists.
- **Execution**: limit orders at your signal's `limit_price` (or LTP/mid),
  repriced toward the touch, NBBO-safe, `orderRef` audit trail
  (`{strategy}:{position_id|new}:{leg}:{side}:{unix_ms}`).
- **Exits**: stop-loss (hybrid 1-min bar trigger + tick-mid catastrophic
  guard), time exit, per-leg independence — all from your config block,
  applied on fill via `_apply_exit_rules`.
- **Attribution**: positions persist to the `position_attribution` table;
  restarts re-seed YOUR strategy's exit rules, not a guess.
- **Control plane**: dashboard exit/pause/flatten work on your positions
  with zero strategy code.
- **Per-day dedup across restarts**: check PositionManager for today's
  positions with your `strategy_id` in `poll()` (see xsp_breakout step 4).

## Rules (will fail review otherwise)

- No broker order calls, no order/position mutation from strategy code.
- Emit `StrategySignal` only; one batch per trade decision.
- Multi-leg strategies: emit one signal per leg with shared metadata
  (see `xsp_short_straddle.py`; legs get independent positions/exits).
- Time semantics: convert to NY via `ZoneInfo("America/New_York")`;
  evaluate entry triggers on completed 1-min bars, not raw quote ticks.
- If your strategy needs config fields the schema lacks, extend
  `src/core/config.py` StrategyConfig with OPTIONAL fields only.
