# IBKR Setup

Use TWS for local paper trading and diagnostics. IB Gateway is also
supported, but TWS makes it easier to visually inspect orders during
development.

## Ports

| App | Paper | Live |
| --- | --- | --- |
| TWS | `7497` | `7496` |
| IB Gateway | `4002` | `4001` |

The logged-in IBKR session determines whether the account is paper or
live. Do not rely on the port number alone.

## TWS Settings

In TWS, open API settings:

```text
Global Configuration -> API -> Settings
```

Set:

- Enable ActiveX and Socket Clients: on
- Read-Only API: off for trading runs
- Socket port: `7497` for paper TWS
- Allow connections from localhost only: on
- Trusted IPs: empty for local-only use

Confirm the TWS window clearly shows paper trading before starting the
runner.

## Broker Config

Paper trading defaults should look like:

```yaml
live_trading:
  enabled: false

connection:
  host: "127.0.0.1"
  port: 7497
  client_id: 9
```

Use a unique client ID for the runner. Diagnostic scripts should use a
different client ID to avoid collisions.

## Diagnostics

Run these before starting the runner:

```bash
python3 scripts/check_ibkr_connection.py --port 7497 --client-id 99
python3 scripts/check_market_data.py --symbol SPY --port 7497
python3 scripts/check_option_chain.py --underlying SPY --port 7497
```

Pass criteria:

- connection succeeds
- expected paper account is visible
- quotes are populated
- option chain lookup returns expiries and strikes

## Market Data

The system needs real-time underlying quotes, option quotes, and option
chain data for configured symbols. Missing, delayed, stale, or invalid
quotes must block trading.

Do not stream full option chains. Select candidate contracts first, then
request quotes only for the contracts needed by the strategy.

Validation should prove:

- underlying bid, ask, and last are real-time
- option bid and ask are populated
- option chain lookup returns usable expiries and strikes
- bid/ask values are non-zero for contracts being considered

Expected failure behavior:

| Issue | Expected behavior |
| --- | --- |
| Delayed data | Reject decisions that depend on the quote |
| Missing bid or ask | Reject the decision that depends on the quote |
| Stale timestamp | Treat quote as unsafe and fail closed |
| Invalid spread | Reject or pause according to risk policy |
| Permission error | Stop accepting new entries until resolved |
| Broker disconnect | Treat freshness as unsafe until new data arrives |

## Live Trading

Live trading is not enabled by changing the port alone. All live gates
must be set intentionally and verified before any live run.

See [Configuration](../reference/configuration.md).
