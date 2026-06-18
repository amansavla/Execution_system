# Configuration

Configuration lives in `configs/` and is loaded by the runner at startup.

## Files

| File | Purpose |
| --- | --- |
| `broker.yaml` | IBKR host, port, client ID, account allowlist, live gates, order defaults |
| `risk.yaml` | Global and per-strategy risk limits |
| `strategies.yaml` | Strategy definitions, entry windows, exit defaults, sizing |
| `symbols.yaml` | Tradable symbols and market-data freshness policy |
| `overrides.yaml` | Manual system and strategy overrides |
| `strategy_overrides.yaml` | Dashboard-persisted strategy parameter changes |
| `dashboard.yaml` | Dashboard host, port, refresh, and panel configuration |

## Load Model

The runner loads base config at startup. Dashboard strategy edits are
persisted to `configs/strategy_overrides.yaml` and applied by the runner's
control path.

Restart the runner after changing base files such as `broker.yaml`,
`risk.yaml`, `strategies.yaml`, or `symbols.yaml`.

## Live Trading Gates

Live trading requires all of the following:

- `live_trading.enabled: true` in `configs/broker.yaml`
- `ALLOW_LIVE_TRADING=I_UNDERSTAND_THIS_CAN_LOSE_MONEY`
- live account ID in the broker allowlist
- passing paper-mode tests
- tested reconciliation, kill switches, flatten, and cancel controls

Leave `live_trading.enabled: false` for paper trading.

## Time Fields

The legacy field `exit.time_exit_utc` is currently interpreted as
America/New_York local time. Treat the name as historical until the config
schema is renamed.

