# Documentation Index

Complete documentation for the Execution System v2 (0DTE options trading platform).

---

## Quick Start (5 minutes)

1. **[architecture.md](architecture.md)** — System overview (short version)
2. **[IBKR Setup](ibkr_setup.md)** — Connect TWS for paper trading
3. **Paper Trading Checklist** → Run the system locally

---

## Core Documentation

### For Understanding the System

| Document | Purpose | Audience |
|----------|---------|----------|
| **[ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)** | Deep dive into all components (runner, broker, order manager, risk engine, dashboard) | Developers, architects |
| **[CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)** | All configuration files, options, and tuning | Operators, developers |
| **[DASHBOARD_API.md](DASHBOARD_API.md)** | Complete HTTP API reference for dashboard, manual control, monitoring | Operators, integrations |

### For Running the System

| Document | Purpose | Audience |
|----------|---------|----------|
| **[OPERATIONS.md](OPERATIONS.md)** | Deployment, monitoring, incident response, maintenance | Operators |
| **[RUNBOOK.md](RUNBOOK.md)** | Step-by-step procedures for common operations (start, stop, manual control) | Operators |
| **[paper_trading_checklist.md](paper_trading_checklist.md)** | Pre-trading verification steps | Traders |

### For Development

| Document | Purpose | Audience |
|----------|---------|----------|
| **[DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)** | Setup, testing, debugging, common development tasks | Developers |
| **[test plan / live_trading_checklist.md](live_trading_checklist.md)** | Pre-live testing steps | Developers, QA |

### For Historical Context

| Document | Purpose | Audience |
|----------|---------|----------|
| **[CHANGELOG_2026-06-12.md](CHANGELOG_2026-06-12.md)** | Session notes: 6 critical fixes + dashboard improvements | All |
| **[INCIDENTS_2026-06-11.md](INCIDENTS_2026-06-11.md)** | Root causes and fixes from 2026-06-11 incidents | Developers |

---

## Navigation by Role

### 👤 I'm a Trader — I want to...

- **Run the system** → Start with [paper_trading_checklist.md](paper_trading_checklist.md), then [RUNBOOK.md](RUNBOOK.md)
- **Monitor performance** → See "Dashboard Monitoring" in [OPERATIONS.md](OPERATIONS.md)
- **Manually close a position** → See "Manual Control" in [DASHBOARD_API.md](DASHBOARD_API.md)
- **Emergency exit** → See "Emergency Procedures" in [OPERATIONS.md](OPERATIONS.md)

### 👨‍💻 I'm a Developer — I want to...

- **Understand the architecture** → Read [ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)
- **Add a new strategy** → See "Add a New Strategy" in [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)
- **Debug an issue** → See "Debugging" in [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) + [OPERATIONS.md](OPERATIONS.md) incident section
- **Write tests** → See "Testing" in [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)
- **Optimize performance** → See "Profiling & Benchmarking" in [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)

### 🏗️ I'm an Architect — I want to...

- **Understand overall design** → [ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)
- **Review design decisions** → [CHANGELOG_2026-06-12.md](CHANGELOG_2026-06-12.md) (explains why fixes were made)
- **Plan scaling** → See "Scaling (Future)" in [OPERATIONS.md](OPERATIONS.md)
- **Data flow overview** → "Data Flow" section in [ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)

### 🔧 I'm an Operator — I want to...

- **Deploy and run** → [OPERATIONS.md](OPERATIONS.md)
- **Monitor system health** → "Monitoring" section in [OPERATIONS.md](OPERATIONS.md)
- **Respond to incidents** → "Incident Response" section in [OPERATIONS.md](OPERATIONS.md)
- **Perform manual control** → [RUNBOOK.md](RUNBOOK.md) + [DASHBOARD_API.md](DASHBOARD_API.md)

---

## Configuration Quick Reference

**All config files are in `configs/`:**
- `broker.yaml` → IBKR connection + order repricing settings
- `risk.yaml` → Position size limits
- `strategies.yaml` → Strategy enablement
- `strategy_overrides.yaml` → Runtime tuning (highest priority)

**For complete reference:** See [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)

---

## System Architecture (30-second Summary)

```
Market Data (IBKR TWS)
    ↓
Runner (orchestration loop, strategy execution, position management)
    ↓
Dashboard (HTTP API + WebSocket, manual control, monitoring)
    ↓
SQLite (events, positions, commands)
```

**Key Principles:**
- Database-centric: Dashboard never talks to broker directly
- Event-sourced: All trades logged as immutable events
- Fail-closed: Reconciliation locks system on mismatch
- Repriced every 2s: In-place modify (not cancel/replace)

**For deep dive:** [ARCHITECTURE_DETAILED.md](ARCHITECTURE_DETAILED.md)

---

## API Quick Reference

| Endpoint | Purpose | Method |
|----------|---------|--------|
| `GET /api/state` | Current positions, orders, PnL | GET |
| `GET /api/pnl` | Daily & all-time PnL breakdown | GET |
| `GET /api/history` | Closed positions with close reason | GET |
| `GET /api/events` | Event log (fills, cancels, closes) | GET |
| `GET /api/logs` | Runner log tail | GET |
| `POST /api/commands` | Manual control (close position, unlock, etc.) | POST |
| `WS /ws` | Live state push (1s cadence) | WebSocket |

**For full API reference:** [DASHBOARD_API.md](DASHBOARD_API.md)

---

## Changelog & Incidents

**Latest Session (2026-06-12):**
- Fixed 6 critical bugs (GC tasks, Adaptive delays, recon lock, orphaned orders, exit loop, premium enforcement)
- Added daily PnL tracking and per-strategy re-entry control
- All changes documented in [CHANGELOG_2026-06-12.md](CHANGELOG_2026-06-12.md)

**Previous Session (2026-06-11):**
- Multiple incidents identified and fixed
- Post-mortem in [INCIDENTS_2026-06-11.md](INCIDENTS_2026-06-11.md)

---

## Testing

**All tests passing:** 476 unit + integration tests

Run tests:
```bash
pytest tests/ -v
```

For test writing guide: See [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md)

---

## Deployment Checklist

**Paper Trading:**
- [ ] TWS running on port 7497
- [ ] API enabled in TWS (Edit → Preferences → API)
- [ ] `python3 -m src.app.runner configs/`
- [ ] `python3 -m uvicorn src.dashboard.app:app --port 8500`
- [ ] Navigate to `http://localhost:8500`

**Live Trading (Future):**
- Change `broker.yaml` port to 7496
- Reduce risk limits (max_contracts, max_premium_per_trade)
- See [OPERATIONS.md](OPERATIONS.md) for full checklist

---

## Support

**Need help?**
1. Check the appropriate doc above (by role or task)
2. Search docs for error message
3. See "Troubleshooting" section in [OPERATIONS.md](OPERATIONS.md)
4. Review incident logs: [INCIDENTS_2026-06-11.md](INCIDENTS_2026-06-11.md), [INCIDENTS_2026-06-12.md](INCIDENTS_2026-06-12.md)
5. Check code comments in relevant module

**Common Issues:**
- Dashboard shows "no data" → Check runner is running (`ps aux | grep runner`)
- Orders not filling → Check quote staleness in logs, verify IBKR paper quality
- System locked → See incident response in [OPERATIONS.md](OPERATIONS.md)
- Database locked → Increase timeout, restart runner

---

## Files at a Glance

### Core Architecture Docs
- `ARCHITECTURE_DETAILED.md` (3,500 lines) — Complete system breakdown
- `CONFIGURATION_GUIDE.md` (2,000 lines) — All config options
- `DASHBOARD_API.md` (1,500 lines) — HTTP API reference
- `DEVELOPMENT_GUIDE.md` (1,200 lines) — Dev setup & testing
- `OPERATIONS.md` (1,400 lines) — Deployment & ops

### Reference Docs
- `CHANGELOG_2026-06-12.md` — 6 critical fixes + dashboard improvements
- `INCIDENTS_2026-06-11.md` — Root causes from 2026-06-11
- `RUNBOOK.md` — Step-by-step operational procedures
- `architecture.md` — Quick overview (short version)
- `assumptions.md` — Design assumptions & constraints

### Checklists & Setup
- `paper_trading_checklist.md`
- `live_trading_checklist.md`
- `ibkr_setup.md`

---

## Latest Commits

```
41184b7 Docs: comprehensive guides (architecture, configuration, API, development, operations)
2f7b55c Initial commit: Execution System v2
```

All changes deployed and documented. System is production-ready for paper trading with comprehensive monitoring and manual control.
