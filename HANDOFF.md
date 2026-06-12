# HANDOFF — Execution System v2 Revamp (Phases 3–7)

Date: 2026-06-11. Repo: `/Users/aman/US strats/Execution_System_v2` (paper only).
The live system at `/Users/aman/US strats/Execution_System` was never touched.

## Commit hashes (this session, in order)

| Hash | Phase | Summary |
|---|---|---|
| `2b415c5` | 3 | Auto-resume after reconnect + persisted position attribution |
| `90407d5` | 4 | SQLite command queue + orderRef audit trail |
| `5722880` | 5 | FastAPI operator dashboard (DB-only, zero broker access) |
| `27c1c97` | 6 | Shadow replay acceptance test |
| `2069d07` | 7 | Multi-strategy validation + plugin contract |

Prior session (context): `8766dec` baseline, `dbc9bbf` Phase 1 (backtest math),
`aa14e12` streaming quotes, `f10ee50` execution quality + slippage,
`cf5fa23` bar engine + hybrid SL.

## Per-phase changes

### Phase 3 — resilience + attribution
- **Auto-resume** (`src/app/runner.py`): disconnect-provenance flag
  `_locked_by_disconnect`; `_try_auto_resume()` runs in `_main_loop` while
  so-locked — broker back → reconcile → clean → `unlock_system()` → resume.
  Mismatch or manual locks are NEVER auto-cleared (fail-closed preserved).
- **Attribution** (`src/storage/position_store.py`, runner seeding/fill
  handler): every fill and every seed upserts `position_attribution`;
  `_seed_positions_from_broker` looks up exact contract identity
  (symbol, expiry, strike, right, status OPEN) → restores true
  `strategy_id` AND `entry_time` (time exits anchor correctly); the
  underlying-match heuristic is fallback only (logged loudly); stale OPEN
  rows swept on startup.

### Phase 4 — command queue + orderRef
- `src/control/command_queue.py`: durable `commands` table; runner drains
  per tick in `_process_commands` (`runner.py`). Routing — NO parallel paths:
  `exit_position` → `_manual_exits` set → ExitManager strategy-exit path;
  `cancel_order` → `OrderManager.cancel_order`; `pause_strategy` →
  `OverrideManager.pause/resume_strategy`; `flatten_all` → latched
  `_flatten_all_active` → ExitManager `force_flatten_all`, released when flat.
- **orderRef** built at construction in `OrderManager.submit_intent`
  (`src/execution/order_manager.py` ~line 115), carried through reprice
  replacements, set on `ib_order.orderRef` (`src/broker/ibkr_broker.py`).

### Phase 5 — dashboard
- `src/storage/runtime_state.py` + `runner._write_runtime_snapshot()`:
  runner publishes positions/marks/PnL/orders/status to `runtime_state`
  each tick.
- `src/dashboard/app.py` (FastAPI) + `static/index.html`: positions table
  w/ Exit buttons, orders w/ Cancel, per-strategy PnL cards, status panel
  (LOCKED/REDUCE-ONLY) + pause/resume + flatten-all, events feed; WebSocket
  `/ws` pushes 1s; `POST /api/commands` → queue. AST-level test forbids
  broker/execution/portfolio/risk imports.
- Run: `DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app --port 8500`

### Phase 6 — shadow replay
- `BarBuilder(persist_dir="data/bars")` appends completed bars to
  `data/bars/bars_YYYYMMDD.jsonl` (wired in ibkr_broker).
- `src/replay/shadow.py` + `scripts/shadow_replay.py`: replays bars
  through NOTEBOOK semantics, diffs vs `execution_quality` fills, writes
  `reports/shadow_<date>.md` + `.diff.log` (JSONL).
- Usage: `python3 scripts/shadow_replay.py 20260612` (after a trading day).

### Phase 7 — multi-strategy
- `tests/integration/test_multi_strategy.py`: attribution correct in
  normal / post-reconnect / post-restart, two strategies, same underlying.
- `configs/strategies.yaml`: `xsp_straddle_1000_20` **enabled**,
  `max_contracts: 2` (validation size; restore to 10 after the live-paper
  validation run). Breakout `xsp_0dte_1000` remains enabled.
- `docs/PLUGIN_CONTRACT.md`: third strategy = provider module + one-line
  composite registration + config block; zero core changes.

## Test commands (final outputs)

- Unit: `python3 -m pytest tests/unit -q` → `424 passed` at orientation;
  final combined run below includes all new tests.
- Full (excl. live-TWS test):
  `python3 -m pytest tests/unit tests/integration -q --ignore=tests/integration/test_ibkr_paper_connection.py`
  → **`456 passed, 1 warning in 14.28s`**
- Phase 3 e2e: `python3 -m pytest tests/integration/test_resilience.py -q` → `4 passed`
- Phase 4 e2e: `python3 -m pytest tests/integration/test_command_queue.py -q` → `4 passed`
- Phase 5: `python3 -m pytest tests/integration/test_dashboard.py -q` → `3 passed`
- Phase 7: `python3 -m pytest tests/integration/test_multi_strategy.py -q` → `3 passed`

## Schemas / contracts

**Command queue** (SQLite, user-approved over HTTP) — table `commands` in
`data/events.db`:
`(command_id TEXT PK, type TEXT, payload TEXT JSON, status pending|done|failed,
created_at TEXT, processed_at TEXT, result TEXT)`.
Types: `exit_position {position_id}`, `cancel_order {order_id}`,
`pause_strategy {strategy_id, paused}`, `flatten_all {}`.

**orderRef**: `{strategy}:{position_id|new}:{leg}:{side}:{unix_ms}`,
leg = `CE`/`PE` from the option right. Deviations from spec (deliberate,
flag if unwanted): (1) leg is ALWAYS included, even single-leg —
deterministic beats conditionally-omitted; (2) entry orders carry `new`
in the position slot because position_id is created at fill time.

**Attribution** — table `position_attribution` in `data/events.db`:
`(position_id TEXT PK, strategy_id, symbol, expiry, strike REAL, right,
side, quantity INT, avg_entry_price REAL, entry_time TEXT, status,
updated_at TEXT)` + index on status. Seeding reads
`find_open_attribution(symbol, expiry, strike, right)` (most recent OPEN).

**Bar engine**: `src/marketdata/bars.py`. Fed from streaming trade prints in
`ibkr_broker._on_pending_ticker` (index underlying falls back to bid/ask
mid). Bars are NY-minute aligned (`ZoneInfo("America/New_York")`,
second=0); a working bar completes when a tick from a later minute
arrives or lazily on read. Persisted per-day JSONL when `persist_dir` set.

**Recovery contract**: on disconnect the runner locks (Hard Rule 7) with
provenance; on reconnect it reconciles internal orders/positions vs
broker; ONLY a clean report unlocks (persisted via `overrides.yaml`);
attribution table re-seeds strategy + entry_time + exit rules on restart.

## Phase 3-seam → (already built through Phase 7)

OverrideManager: `src/control/overrides.py`. ExitManager:
`src/portfolio/exit_manager.py`. OrderManager: `src/execution/order_manager.py`.
Automated exit flow: runner `_check_exits` → `ExitManager.check_exits`
(bars + quotes + strategy_exits/force flags) → exit `OrderIntent` →
permissive RiskDecision → `OrderManager.submit_intent` (aggressive
reprice cfg) → broker. Dashboard commands enter exactly this flow via
`_manual_exits` / `_flatten_all_active` — proven identical in
`test_command_queue.py::test_dashboard_exit_identical_to_automated_exit`.

## Open questions / deferred decisions

1. **Hybrid-SL guard buffer = 15% of the stop LEVEL** (`ExitManager.
   INTRABAR_GUARD_BUFFER`, stop×(1±0.15) directionally away from entry).
   Carried from Phase 2; if you prefer fraction-of-stop-distance units,
   that's a small change in `exit_manager.py`.
2. **Breakout bar-fallback**: `xsp_breakout.py` falls back to the live
   quote when no completed bar exists yet (first minute after startup).
   Strict bar-only would delay signals ~1 min after restart. Your call.
3. **Legacy Streamlit dashboard** (`src/dashboard/streamlit_app.py`,
   `views/`) instantiates its own managers — the exact anti-pattern
   Phase 5 eliminates. Left untouched per no-refactoring rule; recommend
   deleting it.
4. **Straddle shadow replay** validates exits only (entry strike selection
   needs the full quote surface, not recorded). If full straddle replay is
   wanted, record the entry-time option quote snapshot at signal time.
5. **`max_contracts: 2` on xsp_straddle_1000_20** must be restored to 10
   after the live-paper validation run.
6. **Live-paper validation run** of both strategies simultaneously still
   needs a market-hours session with v2 + TWS (ask-first gate: starting it
   is fine off-hours; it trades at 10:00 NY). Integration tests cover the
   logic; the live run is operational confirmation.
7. Timing-sensitive integration tests use real `asyncio.sleep`s; if CI
   flakes appear, raise the 0.3–0.5s waits.
