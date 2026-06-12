# Assumptions

All assumptions about broker behavior, market data, or design
decisions not explicitly covered by AGENTS.md are documented here.
Each entry includes a date and the phase in which it was recorded.

## Phase 0

- No assumptions recorded in Phase 0.

## Phase 1

- **P1-A1 (2026-05-20):** `QuoteSnapshot.bid` and `QuoteSnapshot.ask`
  are modeled as `Optional[float]`. A missing bid or ask (None) indicates
  the quote is incomplete or unavailable. Any component consuming a
  QuoteSnapshot must check for None before using price data. This is
  distinct from a stale quote (which has a valid price but an old
  timestamp).

- **P1-A2 (2026-05-20):** `QuoteSnapshot.timestamp` is modeled as a
  naive UTC datetime. We assume all timestamps flowing through the
  system are UTC. Timezone-aware datetimes may be introduced in a
  later phase if broker callbacks provide them, but for now the
  contract is: all datetimes are UTC, stored as naive datetime objects.

- **P1-A3 (2026-05-20):** `OptionContract.strike` is modeled as
  `float`. Some underlyings (SPX) have integer strikes, but float
  covers all cases and matches IBKR's representation. Precision
  issues with float strikes are not expected for the underlyings in
  scope (SPX, XSP, QQQ) but this assumption should be revisited if
  sub-penny strikes are ever encountered.

- **P1-A4 (2026-05-20):** `FillEvent.commission` is modeled as
  `Optional[float]` because IBKR may not report commission immediately
  with the fill callback. Downstream PnL calculations must handle
  None commission gracefully.

- **P1-A5 (2026-05-20):** `Position.average_entry_price` is modeled
  as `float`. For multi-fill entries, the PositionManager (Phase 6)
  will be responsible for computing the volume-weighted average. The
  model itself does not enforce this calculation.

- **P1-A6 (2026-05-20):** The `RiskDecision` model includes an
  `allowed_quantity` field. Per AGENTS.md, RiskEngine may reduce
  quantity but never increase it. This invariant is not enforced at
  the model level — it will be enforced in RiskEngine logic (Phase 3).

- **P1-A7 (2026-05-20):** `ExecutionReport` and `ReconciliationReport`
  are modeled as summary snapshots. The exact fields needed may evolve
  as Phases 8 and 10 are implemented. The Phase 1 models capture the
  minimum structural contract.

## Phase 2

- **P2-A1 (2026-05-20):** Config YAML files that are empty or
  all-comments are treated as valid for optional configs (strategies,
  symbols, overrides, dashboard) by returning safe defaults (empty
  lists, disabled flags). Required configs (risk.yaml) raise an
  explicit error when empty, since all risk fields are mandatory.

- **P2-A2 (2026-05-20):** `force_flatten_time_utc` is optional in
  risk.yaml. When present, it must be strictly after
  `no_new_trades_cutoff_utc`. When absent, the system has no automatic
  force-flatten — this will need to be handled by ExitManager (Phase 6)
  using time_exit from strategy config instead.

- **P2-A3 (2026-05-20):** Time fields (`no_new_trades_cutoff_utc`,
  `force_flatten_time_utc`, `time_exit_utc`, trading hours) are stored
  as HH:MM strings in YAML and validated at load time. They are parsed
  to `datetime.time` objects via properties. This avoids YAML parsing
  ambiguity (YAML 1.1 can interpret bare `19:30` as seconds).

- **P2-A4 (2026-05-20):** The `overrides.yaml` loader supports both
  a nested format (`overrides: { ... }`) and a flat format at the
  top level. This flexibility accommodates different editing styles
  without breaking the loader.

- **P2-A5 (2026-05-20):** `StrategyConfig.dte_target` is constrained
  to 0 or 1 (ge=0, le=1) since AGENTS.md specifies "0DTE/1DTE"
  strategies. If longer-dated strategies are needed later, this
  constraint should be relaxed.

- **P2-A6 (2026-05-20):** The Phase 1 `RiskConfig` model in
  `models.py` is a minimal runtime representation. The Phase 2
  `FullRiskConfig` in `config.py` is the full YAML-shaped config
  with all sections. Downstream phases should use `FullRiskConfig`
  for loading and can extract fields as needed for the runtime
  `RiskConfig` model.

## Phase 3

- **P3-A1 (2026-05-20):** RiskEngine receives all external state via
  an immutable `SystemState` dataclass. It never queries external
  systems directly. This makes it fully deterministic and testable
  without any mocks. The caller (runner, Phase 15) is responsible for
  assembling the SystemState snapshot before each evaluation.

- **P3-A2 (2026-05-20):** `PositionInfo` is a lightweight dataclass
  used in SystemState instead of the full `Position` model. This keeps
  the RiskEngine interface minimal and avoids importing heavy models
  that could create circular dependencies in later phases.

- **P3-A3 (2026-05-20):** The RiskEngine does not short-circuit on
  the first blocking reason. It collects all violations so the caller
  (and operator) can see the complete picture. This is important for
  debugging why a signal was rejected during live operation.

- **P3-A4 (2026-05-20):** `daily_pnl` is a negative number when
  losing money. The check is `daily_pnl <= -daily_loss_limit`. This
  means a limit of $5000 blocks when PnL reaches -$5000 or worse.
  This matches the intuitive meaning of "daily loss limit."

- **P3-A5 (2026-05-20):** The cutoff time check uses `>=` (greater
  than or equal). At exactly 19:30 UTC, new trades are blocked. This
  is the conservative choice — fail closed at the boundary.

- **P3-A6 (2026-05-20):** Quantity clamping: when `requested_quantity`
  exceeds `max_contracts_per_trade`, the RiskEngine approves with a
  reduced `allowed_quantity` and emits a warning. It does not reject.
  Per AGENTS.md: "RiskEngine may reduce it, never increase it."

- **P3-A7 (2026-05-20):** The AGENTS.md lists several checks not yet
  fully implemented in Phase 3 because they depend on later phases:
  `duplicate_exposure` (needs position history), `buying_power_estimate`
  (needs account state integration), `contract_qualification` (needs
  option chain data from Phase 7), `max_premium_per_trade` (needs
  quote-based premium calculation). These are documented here as
  known gaps and will be implemented as their dependencies become
  available. The RiskEngine architecture supports adding checks
  without changing the public API.

## Phase 4

- **P4-A1 (2026-05-20):** MockBrokerClient simulates asynchronous order flow using `asyncio.create_task` and customizable sleeps. This matches the async nature of real broker systems (like IBKR) where status updates arrive via asynchronous callbacks rather than immediate blocking replies.
- **P4-A2 (2026-05-20):** If configured with custom preset `simulated_positions`, `MockBrokerClient` will prioritize returning them in `get_positions()` to facilitate manual position overrides (for testing reconciliation failures). Otherwise, it uses dynamically tracked positions based on actual order fill simulation.
- **P4-A3 (2026-05-20):** Callback handlers registered via `register_order_callback`, `register_fill_callback`, and `register_quote_callback` are wrapped in try-except blocks to ensure individual subscriber failures do not interrupt the core simulation loop.
- **P4-A4 (2026-05-20):** Simulated positions dynamically tracked by order fills are simplified: buying a contract increases position size, selling it reduces it. If a position quantity drops to zero, the position object is removed from the dynamic tracking database.
- **P4-A5 (2026-05-20):** `EventStore` is represented as a lightweight `EventStoreStub` collector in this phase. Real EventStore persistence (sqlite/file) is scheduled for Phase 8.

## Phase 5

- **P5-A1 (2026-05-20):** OrderManager uses `asyncio.create_task` to handle cancellation requests and repricing workflows asynchronously, ensuring callback execution loops are not blocked.
- **P5-A2 (2026-05-20):** Repricing logic uses a cancel-and-replace strategy: it cancels the active limit order, waits for status transition to `CANCELLED`, and submits a new order for the remaining quantity with the updated limit price.
- **P5-A3 (2026-05-20):** BUY orders are assumed to represent entry orders for long strategy signals. Therefore, upon receiving a `PARTIALLY_FILLED` update for a BUY order, the remainder is automatically cancelled per the partial fill rules in AGENTS.md.
- **P5-A4 (2026-05-20):** Duplicate submission checks rely on the `position_id` field. Multiple active orders for the same `position_id` are disallowed. Orders are considered active if their status is `NEW`, `RISK_CHECKED`, `SUBMITTED`, `PARTIALLY_FILLED`, or `CANCEL_PENDING`.
- **P5-A5 (2026-05-20):** Repricing triggers on price differences of at least $0.01 (one penny) between the current limit price and the quote price. Prices are capped/floored by `max_acceptable_buy_price` and `min_acceptable_sell_price`.

## Phase 6

- **P6-A1 (2026-05-20):** Position updates and state transitions in `PositionManager` are driven purely by `FillEvent` inputs. This isolates state calculations from internal order manager logic or broker client states.
- **P6-A2 (2026-05-20):** For options contract PnL calculations, we apply the standard contract multiplier of 100 (or the explicit `contract.multiplier` if defined) to both realized and unrealized PnL.
- **P6-A3 (2026-05-20):** `ExitManager` does not execute trades directly. It emits `OrderIntent` objects with `is_entry = False` (marking them as reduce-only exits) which are then processed by the execution runner/engine.
- **P6-A4 (2026-05-20):** Exit conditions are evaluated using bid prices for long position liquidations (selling) and ask prices for short position liquidations (buying back), falling back to mid-point prices when the specific bid/ask quote data is missing.
- **P6-A5 (2026-05-20):** Stop-loss and take-profit targets are checked against the current market quote price, and time-exits are triggered when the system clock matches or exceeds `time_exit_utc`.

## Phase 7

- **P7-A1 (2026-05-20):** Days-to-Expiration (DTE) is calculated by parsing the contract's `expiry` string (format YYYYMMDD) and comparing it to the system date in UTC.
- **P7-A2 (2026-05-20):** If `target_delta` is requested, OptionContractSelector requires a non-None `delta` value on the contract's `QuoteSnapshot`, rejecting any contracts where delta is unavailable.
- **P7-A3 (2026-05-20):** Delta comparisons use absolute values (`abs(abs(quote.delta) - abs(target_delta))`) to evaluate closest match, allowing it to work uniformly for both CALL (positive delta) and PUT (negative delta) contracts.
- **P7-A4 (2026-05-20):** Quote validations (freshness, spreads, incompleteness) are consolidated in a reusable module `src/marketdata/data_quality.py`, shared between `OptionContractSelector` and `RiskEngine` to ensure identical data filtering rules.
- **P7-A5 (2026-05-20):** Under data quality constraints, any candidate contract quote that fails freshness, spread, or completeness limits is explicitly rejected rather than silently ignored, enabling detailed rejection logging for strategy diagnostics.

## Phase 8

- **P8-A1 (2026-05-20):** EventStore uses an asyncio queue for non-blocking writes. `log_callback()` is synchronous (enqueues to an internal queue), and a background `asyncio.Task` drains the queue to SQLite. This ensures broker callbacks and order manager helpers never block on database I/O.
- **P8-A2 (2026-05-20):** EventStore maintains a backward-compatible `.events` list (in-memory) in addition to SQLite persistence. This allows existing tests that inspect `.events` directly to continue working without modification to their assertion logic.
- **P8-A3 (2026-05-20):** `strategy_id` is extracted from the event payload dict (if present) and stored as a top-level indexed column. Events without a `strategy_id` key in their payload are stored with `NULL` in the strategy_id column.
- **P8-A4 (2026-05-20):** SQLite uses WAL (Write-Ahead Logging) journal mode for better concurrent read/write performance. This is set on connection creation.
- **P8-A5 (2026-05-20):** The event type taxonomy includes both the AGENTS.md-specified types (`signal`, `risk_decision`, `order_event`, `fill_event`, `position_update`, `exit_decision`, `manual_override`, `reconciliation_event`, `error`) and internal lifecycle types used by existing code (`order_callback`, `fill_callback`, `order_state_transition`, `fill_received`, `position_opened`, etc.). These internal types preserve compatibility with Phase 4-6 logging calls.
- **P8-A6 (2026-05-20):** `EventStoreStub` is retained in `mock_broker.py` but marked as deprecated. It is no longer used by `MockBrokerClient` (which now uses `EventStore`) but remains importable for any external code that may reference it.
- **P8-A7 (2026-05-20):** Pydantic models are serialized via `model_dump(mode="json")` for payload storage, which converts UUIDs and datetimes to JSON-safe strings. Dict payloads are serialized via `json.dumps(default=str)` as a fallback.

## Phase 9

- **P9-A1 (2026-05-20):** ManualControlService never calls BrokerClient directly. All flatten actions route through `PositionManager.force_close_position()` and all cancel actions route through `OrderManager.cancel_order()`. This enforces the component boundary table in AGENTS.md.
- **P9-A2 (2026-05-20):** OverrideManager keeps override state in-memory for fast access and optionally persists to `configs/overrides.yaml` on mutation. The in-memory state is authoritative at runtime; the YAML file is for crash recovery and operator visibility.
- **P9-A3 (2026-05-20):** Every manual control command (including read-only `show-*` and `status` commands) is logged to EventStore as a `manual_override` event. This provides a complete audit trail of operator actions per AGENTS.md rule 10.
- **P9-A4 (2026-05-20):** The `flatten-position` and `flatten-strategy` commands accept an `exit_price` parameter because `PositionManager.force_close_position()` requires an exit price for PnL calculation. In production, this would be the current market price; the CLI requires it explicitly to avoid hidden assumptions.
- **P9-A5 (2026-05-20):** The CLI entry point (`src/app/control.py`) defines the parser and dispatcher but does not wire up live dependencies. The runner (Phase 15) will provide the running system context and construct ManualControlService with real components.
- **P9-A6 (2026-05-20):** `show-rejections` scans the in-memory `.events` list on EventStore rather than querying SQLite. This is sufficient for intraday use where the system restarts daily. For historical queries across sessions, the SQLite query API is available.
- **P9-A7 (2026-05-20):** Override mutations are idempotent: pausing an already-paused strategy returns `"already_paused"` rather than raising an error. This makes manual commands safe to retry.

## Phase 10

- **P10-A1 (2026-05-20):** Reconciliation compares internal and broker state by grouping open positions by option contract and comparing net quantity (using +qty for BUY and -qty for SELL). This groups positions across all strategies to match the broker's unified accounting.
- **P10-A2 (2026-05-20):** If there is any mismatch, we activate `system_locked = True` in `OverrideManager`. This immediately blocks all entry/exit orders system-wide.
- **P10-A3 (2026-05-20):** Active orders are matched primarily via `broker_order_id`. If an order exists at the broker but not internally, or if fields (limit price, side, quantity, contract) do not match, it is flagged as a mismatch.
- **P10-A4 (2026-05-20):** Internal active orders with a `broker_order_id` that are completely missing at the broker are flagged as a mismatch. This protects against orders that were dropped, expired, or cancelled at the broker without our state catching up.
- **P10-A5 (2026-05-20):** A `ReconciliationReport` is logged to the `EventStore` on every run of the `ReconciliationEngine` (under the `reconciliation_event` type) regardless of whether it passes or fails, providing a continuous audit log.

## Phase 11

- **P11-A1 (2026-05-20):** Diagnostic scripts connection parameters default to standard TWS Paper port `7497` and host `127.0.0.1` but allow user overrides via CLI arguments (`--host`, `--port`, `--client-id`) to support customized setups.
- **P11-A2 (2026-05-20):** Diagnostic scripts place zero orders and only invoke read-only methods (`connectAsync`, `managedAccounts`, `accountSummary`, `reqMktData`, `reqSecDefOptParamsAsync`, `qualifyContracts`) to prevent trade risk during setup checks.
- **P11-A3 (2026-05-20):** Real-time market data (Type 1) is used by default in `check_market_data.py`. If real-time quotes are not populated (e.g. out of market hours or missing subscription), the script attempts a fallback request to delayed data (Type 3) to test API permission routing.
- **P11-A4 (2026-05-20):** `check_option_chain.py` uses `reqSecDefOptParamsAsync` rather than requesting the complete contract details for all options contracts, which avoids API request limits and latency spikes.

## Phase 12

- **P12-A1 (2026-05-20):** `IBKRBrokerClient` implements the exact `BrokerClient` interface. It manages a persistent connection via `ib_async.IB()` and handles reconnects automatically in the background using `ib.disconnectedEvent` when configured.
- **P12-A2 (2026-05-20):** Contract qualification (`ib.qualifyContractsAsync`) is performed on demand before any order submission or quote subscription. A local `_qualified_contracts` dictionary caches qualified contracts to optimize performance and prevent duplicate network roundtrips.
- **P12-A3 (2026-05-20):** Safety gate: `place_order` checks the connected accounts list; if any account lacks the `"DU"` paper prefix, and live trading is disabled in `configs/broker.yaml`, it raises a `RuntimeError` to block live order placement.
- **P12-A4 (2026-05-20):** Limit orders are enforced. Market orders are only allowed if `order_type == "MKT"` and the `emergency_flatten` flag is passed as `True`.
- **P12-A5 (2026-05-20):** Raw callbacks (`orderStatusEvent` and `execDetailsEvent`) are logged as `order_callback` and `fill_callback` to `EventStore` in JSON-safe representations, and converted to internal `OrderEvent`/`FillEvent` and `OrderState` models for application handlers.
- **P12-A6 (2026-05-20):** The integration test scans standard paper ports (`7497` and `4002`) and connects if one is listening, skipping gracefully using `pytest.skip` if TWS/Gateway is not active.

## Phase 13

- **P13-A1 (2026-05-20):** In-memory event list compatibility is maintained so that both SQLite DB persistent records and raw list dictionaries inside EventStore can be normalized and queryable, allowing fast and fully isolated unit testing of execution metrics.
- **P13-A2 (2026-05-20):** Mid-price lookup searches backward from the order's submission/arrival timestamp to find the closest preceding quote snapshot or signal event matching the option contract (or underlying symbol). Slippage is calculated relative to this mid-price.
- **P13-A3 (2026-05-20):** Realized PnL is tracked using FIFO inventory valuation per strategy and underlying contract, matching opposite-signed fills to realize profit/loss. Any unmatched fill quantity is added to the inventory queue for future matches.

## Phase 14

- **P14-A1 (2026-05-20):** Streamlit dashboard loads database events using `asyncio.run` to allow background query processing during layout render cycles without blocking page render ticks.
- **P14-A2 (2026-05-20):** The state reconstruction plays back SQLite transaction histories to instantiate memory representations of `PositionManager` and `OrderManager`, enabling the dashboard to function seamlessly even when the live runner process is offline.
- **P14-A3 (2026-05-20):** Safety confirmations are enforced for all critical actions (lock, cancel-all, flatten) using Streamlit's interactive checkbox widget prior to dispatching commands to `ManualControlService`.

## Phase 15

- **P15-A1 (2026-05-21):** ExecutionRunner coordinates the live loop by initializing, running startup reconciliation, and then periodically executing the poll loop. If the initial reconciliation check fails, the runner status is marked as `ERROR`, which halts new trading entries.
- **P15-A2 (2026-05-21):** The runner registers a specialized fill handler callback to ensure that as fills arrive from the broker, the updates are propagated to both `OrderManager` and `PositionManager` in order.
- **P15-A3 (2026-05-21):** Polling intervals (`tick_interval_seconds`) and reconciliation intervals (`reconciliation_interval_seconds`) are fully configurable, defaulting to 1 second and 60 seconds respectively for live paper operations, and parameterized to shorter durations in testing.

## Phase 16

- **P16-A1 (2026-05-21):** The environment variable `ALLOW_LIVE_TRADING` must match exactly the string `"I_UNDERSTAND_THIS_CAN_LOSE_MONEY"`. It is checked pre-connection to prevent accidental network socket initiation.
- **P16-A2 (2026-05-21):** Post-connection safety checks query `self.ib.managedAccounts()` to verify that the active account(s) are strictly in the configuration's `account.allowlist`. If any mismatch occurs, the client immediately issues `self.ib.disconnect()` before raising to prevent socket leaks.
- **P16-A3 (2026-05-21):** When live trading is disabled, paper mode verification expects all accounts to match `"DU"` or `"DF"`. Connecting to any account outside this scope is treated as an invalid live connection, resulting in immediate disconnection.
