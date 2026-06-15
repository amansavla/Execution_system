# Strategy Plugin Contract

Strategies generate trade intent. They do not own orders, fills, broker
state, positions, exits, or risk decisions.

## Provider Interface

Strategy providers subclass `StrategyProvider` from `src/app/runner.py`:

```python
from src.app.runner import StrategyProvider

class MyStrategyProvider(StrategyProvider):
    async def poll(self, strategy_config, current_time):
        return []
```

`poll()` returns a list of `StrategySignal` objects. An empty list means
no trade decision for the current tick.

## Registration

Register the provider in `run_paper_trading.py` through the
`CompositeStrategyProvider` map:

```python
strategy_provider = CompositeStrategyProvider({
    "my_signal_source": MyStrategyProvider(...),
})
```

The key must match `entry.signal_source` in `configs/strategies.yaml`.

## Strategy Config

Example:

```yaml
- strategy_id: "my_strategy"
  enabled: true
  underlying: "XSP"
  option_type: "single"
  direction: "long"
  dte_target: 0
  entry:
    signal_source: "my_signal_source"
    max_contracts: 1
    limit_price_offset: 0.05
    order_timeout_seconds: 90
  exit:
    stop_loss_pct: 30
    time_exit_utc: "15:20"
```

`time_exit_utc` is a legacy field name and is currently interpreted as
America/New_York local time.

## Rules

- Emit `StrategySignal` objects only.
- Do not submit, cancel, replace, or inspect broker orders from strategy
  code.
- Do not mutate positions, fills, risk state, or overrides.
- Use optional config fields when extending strategy behavior.
- Multi-leg strategies should emit one signal per leg with shared
  correlation metadata.
- Time-based triggers should use completed bars where the strategy logic
  depends on bar close semantics.

## Current Implementation Note

Some existing providers are constructed with the broker client for
market-data reads. Treat that as current implementation debt, not a reason
to add order or state management to strategies. New work should keep the
strategy surface as narrow as practical and route execution through the
runner pipeline.

