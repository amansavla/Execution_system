# Quickstart

Use this path for local development and paper trading validation.

## Install

```bash
python3 -m pip install -e ".[dev]"
```

## Run Tests

```bash
python3 -m pytest tests/unit -q
python3 -m pytest tests/unit tests/integration -q --ignore=tests/integration/test_ibkr_paper_connection.py
```

The IBKR paper integration test requires TWS or IB Gateway:

```bash
python3 -m pytest tests/integration/test_ibkr_paper_connection.py -q
```

## Run Paper Trading

Prerequisites:

- TWS or IB Gateway is logged into a paper account.
- API access is enabled.
- Paper port is configured in `configs/broker.yaml`, usually `7497` for
  TWS paper.
- `configs/broker.yaml` has `live_trading.enabled: false`.

Start the supervised runner:

```bash
nohup ./scripts/run_supervised.sh >> data/supervisor.log 2>&1 &
```

Start the dashboard:

```bash
DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app --port 8500
```

Open the dashboard at:

```text
http://localhost:8500
```

## Stop

Prefer the dashboard shutdown control. Manual fallback:

```bash
pkill -f run_paper_trading.py
```

Stopping the runner does not flatten broker positions by itself. Confirm
positions and orders in TWS before walking away.

