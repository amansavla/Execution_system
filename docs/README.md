# Execution System Docs

This GitBook is the operator and engineering handbook for the intraday
options execution system.

The system runs pre-built strategies against Interactive Brokers. It does
not invent signals. The runner owns broker access; the dashboard is a
database-only control plane.

## Start Here

- [Quickstart](getting-started/quickstart.md): install, test, and run the
  paper system locally.
- [IBKR setup](getting-started/ibkr-setup.md): TWS/Gateway settings and
  diagnostic scripts.
- [Architecture overview](architecture/overview.md): process model,
  execution flow, storage, and safety boundaries.
- [Runbook](operations/runbook.md): start, stop, locked mode, daily checks,
  and known operational edges.

## Safety Model

Normal entries and exits use limit orders. Live trading is disabled by
default and requires all safety gates to pass at the same time. If broker
state, quotes, or reconciliation are uncertain, the system fails closed by
blocking new entries while preserving exit and manual control paths.

The top-level `AGENTS.md` remains the authoritative product and safety
specification. These docs explain how to operate and extend the current
implementation.

