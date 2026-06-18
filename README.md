# Intraday Options Execution System

A modular, production-grade intraday options execution system for
Interactive Brokers. It executes pre-built systematic strategies on
US equity underlyings (XSP, QQQ, SPX range). It does not invent
signals or trading rules.

## Architecture Overview

```
┌─────────────┐
│  Strategy    │── emits StrategySignal ──┐
└─────────────┘                           │
                                          ▼
                                   ┌─────────────┐
                                   │  RiskEngine  │── approve / reject
                                   └──────┬──────┘
                                          │ RiskDecision
                                          ▼
                                   ┌─────────────┐
                                   │ OrderManager │── submits via BrokerClient
                                   └──────┬──────┘
                                          │ fills / events
                                          ▼
                                ┌──────────────────┐
                                │ PositionManager   │
                                │ ExitManager       │
                                └──────────────────┘
```

**Key boundaries:**
- Strategies only emit signals — they never touch the broker.
- Every order passes through RiskEngine before submission.
- Limit orders only for normal entries/exits; market orders only in
  emergency flatten mode.
- The system fails closed on stale quotes, disconnects, or unknown state.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| Models | pydantic >= 2.0 (v2 only) |
| Broker | ib_async >= 1.0 |
| Storage | SQLite via aiosqlite |
| Config | YAML (pyyaml) |
| Tests | pytest |
| Async | asyncio, single event loop |

## Phase Status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Repo structure, configs, docs stubs | ✅ complete |
| 1 | Core models and validation tests | ✅ complete |
| 2 | Config loading and validation | ✅ complete |
| 3 | RiskEngine (deterministic, no broker) | ✅ complete |
| 4 | MockBrokerClient | ✅ complete |
| 5 | OrderManager | ✅ complete |
| 6 | PositionManager and ExitManager | ✅ complete |
| 7 | OptionContractSelector | ✅ complete |
| 8 | EventStore and audit logs | ✅ complete |
| 9 | ManualControlService and CLI | ✅ complete |
| 10 | ReconciliationEngine | ✅ complete |
| 11 | IBKR setup docs and diagnostic scripts | ✅ complete |
| 12 | IBKRBrokerClient, paper mode | ✅ complete |
| 13 | Execution analytics | ✅ complete |
| 14 | FastAPI dashboard | ✅ complete |
| 15 | Paper trading runner | ✅ complete |
| 16 | Live mode safety gates | ✅ complete |


## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Copy and configure environment
cp .env.example .env
```

## Safety

Live trading is **DISABLED** by default and requires ALL safety gates
to pass simultaneously. See AGENTS.md for the full list of gates.
