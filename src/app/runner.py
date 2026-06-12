"""ExecutionRunner — main paper trading loop.

Wires all components together and runs the poll-based execution loop
on a single asyncio event loop per AGENTS.md § "Signal dispatch".

Lifecycle:
    1. Load configs
    2. Start EventStore
    3. Connect broker
    4. Run reconciliation (must pass before trading)
    5. Poll loop: for each tick
       a. Poll strategies for signals
       b. For each signal: contract selection → risk check → order submission
       c. Check ExitManager for open positions
       d. Process pending fills via callbacks
       e. Periodic reconciliation
    6. Clean shutdown on stop()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from src.app.status import RunnerStatus
from src.broker.interface import BrokerClient
from src.control.manual_control import ManualControlService
from src.control.overrides import OverrideManager
from src.core.config import (
    FullRiskConfig,
    OverridesConfig,
    StrategyConfig,
    load_broker_config,
    load_overrides_config,
    load_risk_config,
    load_strategies_config,
)
from src.core.enums import (
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)
from src.core.models import (
    OrderIntent,
    OrderState,
    Position,
    QuoteSnapshot,
    RiskDecision,
    StrategySignal,
)
from src.execution.order_manager import OrderManager, RepriceConfig
from src.portfolio.exit_manager import ExitManager
from src.portfolio.position_manager import PositionManager
from src.portfolio.reconciliation import ReconciliationEngine
from src.risk.risk_engine import PositionInfo, RiskEngine, SystemState
from src.control.command_queue import CommandQueue
from src.storage.event_log import EventStore
from src.storage.position_store import PositionStore
from src.storage.runtime_state import RuntimeStateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy provider protocol
# ---------------------------------------------------------------------------

class StrategyProvider:
    """Interface that the runner calls to poll strategies for signals.

    Users should subclass this or provide a callable that returns
    a list of StrategySignals for the current tick.
    """

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Return signals from a strategy for this tick. Empty list = no signal."""
        return []

    async def collect_exits(self, strategy_config: StrategyConfig, current_time: datetime) -> set[UUID]:
        """Position IDs this strategy wants closed NOW (strategy-driven exits).

        Called every exit-check tick for strategies with open positions —
        unlike poll(), which the runner skips while a strategy has open
        positions/orders (wait-until-flat). Returned ids ride ExitManager's
        `strategy_exit` path. Default: no strategy-driven exits.
        """
        return set()


# ---------------------------------------------------------------------------
# ExecutionRunner
# ---------------------------------------------------------------------------

class ExecutionRunner:
    """Main paper-trading execution loop.

    Wires together all components and orchestrates the poll loop.
    Designed to work with MockBrokerClient for paper simulation.
    """

    def __init__(
        self,
        broker: BrokerClient,
        event_store: EventStore,
        strategy_provider: Optional[StrategyProvider] = None,
        configs_dir: Path = Path("configs"),
        tick_interval_seconds: float = 1.0,
        reconciliation_interval_seconds: float = 60.0,
    ) -> None:
        self.broker = broker
        self.event_store = event_store
        self._strategy_provider = strategy_provider or StrategyProvider()
        self._configs_dir = configs_dir
        self._tick_interval = tick_interval_seconds
        self._reconciliation_interval = reconciliation_interval_seconds

        # Status
        self.status = RunnerStatus.INITIALIZING
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused initially

        # Components (initialized in _load_configs)
        self.risk_engine: Optional[RiskEngine] = None
        self.order_manager: Optional[OrderManager] = None
        self.position_manager: Optional[PositionManager] = None
        self.exit_manager: Optional[ExitManager] = None
        self.override_manager: Optional[OverrideManager] = None
        self.reconciliation_engine: Optional[ReconciliationEngine] = None
        self.manual_control: Optional[ManualControlService] = None

        # Config objects
        self._risk_config: Optional[FullRiskConfig] = None
        self._strategy_configs: dict[str, StrategyConfig] = {}
        self._overrides_config: Optional[OverridesConfig] = None

        # Tick counter
        self._tick_count = 0
        self._last_reconciliation: Optional[datetime] = None
        self._asymmetric_exits: set[UUID] = set()
        # Exit-order retry throttle: position_id -> (attempts, last_attempt).
        # A broker-rejected exit goes terminal instantly, making the position
        # "eligible" again next tick — without a backoff that re-submits a
        # doomed order every second (observed live 2026-06-12: 14 rejected
        # exits in 14s when an orphaned entry blocked the opposite side).
        self._exit_attempts: dict[UUID, tuple[int, datetime]] = {}

        # Position->strategy attribution persistence (survives restarts)
        self.position_store = PositionStore(
            getattr(event_store, "_db_path", ":memory:")
        )

        # True only when the system lock was set by the broker-disconnect
        # path. Auto-resume may ONLY clear locks with this provenance —
        # manual/reconciliation locks always require human action.
        self._locked_by_disconnect = False

        # Control plane (dashboard -> runner). Commands flow through the
        # exact same paths as automated actions: manual exits ride the
        # ExitManager strategy_exit path; flatten_all latches the
        # force_flatten path until the book is flat.
        self.command_queue = CommandQueue(
            getattr(event_store, "_db_path", ":memory:")
        )
        self._manual_exits: set[UUID] = set()
        self._flatten_all_active = False
        # position_id -> exit trigger ("stop_loss"/"time_exit"/"manual"/...)
        # recorded when the exit order goes out; persisted with the closed
        # position so the dashboard can show WHY a position was closed.
        self._exit_reasons: dict[UUID, str] = {}

        # Runtime snapshot for the dashboard (DB-only consumer)
        self.runtime_state_store = RuntimeStateStore(
            getattr(event_store, "_db_path", ":memory:")
        )

        # One-trade-per-day enforcement that SURVIVES RESTARTS: strategy_ids
        # with any persisted position entered today (NY date). Strategy
        # providers keep their own in-memory sets, but those are lost on
        # restart, which let straddles re-enter mid-session (2026-06-11).
        self._persisted_traded_today: set[str] = set()
        self._traded_today_ny_date: Optional[str] = None
        # strategy_id -> earliest next entry attempt after a rejected batch
        # (prevents the emit->reject->emit spam loop at 1 signal/sec).
        self._entry_retry_after: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_configs(self) -> None:
        """Load all YAML configs and wire up components."""
        logger.info("Loading configurations from %s", self._configs_dir)

        # Load risk config
        risk_path = self._configs_dir / "risk.yaml"
        if risk_path.exists():
            self._risk_config = load_risk_config(risk_path)
        else:
            raise FileNotFoundError(f"Required config missing: {risk_path}")

        # Load strategies config
        strategies_path = self._configs_dir / "strategies.yaml"
        if strategies_path.exists():
            strategies_cfg = load_strategies_config(strategies_path)
            self._strategy_configs = {
                s.strategy_id: s for s in strategies_cfg.strategies
            }
        else:
            self._strategy_configs = {}

        # Load overrides config
        overrides_path = self._configs_dir / "overrides.yaml"
        if overrides_path.exists():
            self._overrides_config = load_overrides_config(overrides_path)
        else:
            self._overrides_config = OverridesConfig()

        logger.info(
            "Loaded configs: %d strategies, risk=%s, overrides=%s",
            len(self._strategy_configs),
            "loaded" if self._risk_config else "missing",
            "loaded",
        )

        # Layer dashboard-made parameter changes over the base configs
        self._apply_strategy_overrides_file()

    def _wire_components(self) -> None:
        """Instantiate and wire all execution components."""
        # Position manager
        self.position_manager = PositionManager(self.event_store)
        if hasattr(self._strategy_provider, "set_position_manager"):
            self._strategy_provider.set_position_manager(self.position_manager)

        # Order manager
        self.order_manager = OrderManager(self.broker, self.event_store)

        # Exit manager
        self.exit_manager = ExitManager(self.event_store)

        # Override manager
        overrides_path = self._configs_dir / "overrides.yaml"
        self.override_manager = OverrideManager(
            initial_state=self._overrides_config,
            persist_path=overrides_path if overrides_path.exists() else None,
        )

        # Risk engine
        self.risk_engine = RiskEngine(
            risk_config=self._risk_config,
            strategy_configs=self._strategy_configs,
            overrides=self.override_manager.state,
        )

        # Reconciliation engine
        self.reconciliation_engine = ReconciliationEngine(
            broker=self.broker,
            position_manager=self.position_manager,
            override_manager=self.override_manager,
            event_store=self.event_store,
        )

        # Manual control service
        self.manual_control = ManualControlService(
            event_store=self.event_store,
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            override_manager=self.override_manager,
            operator="runner",
        )

        # Wire fill callback chain
        self.register_fill_handler()

        logger.info("All components wired successfully")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize, reconcile, and start the main loop."""
        self.status = RunnerStatus.INITIALIZING
        self.event_store.log_callback("runner_lifecycle", {
            "action": "starting",
            "timestamp": datetime.now(UTC).isoformat(),
        })

        # 1. Start event store
        await self.event_store.start()

        # 2. Load configs
        self._load_configs()
        self._wire_components()

        # 3. Connect broker
        await self.broker.connect()
        logger.info("Broker connected")

        # 3.5 Seed PositionManager from any pre-existing broker positions
        # (e.g. positions opened before a restart) so they are managed by
        # ExitManager from tick 1, before reconciliation runs.
        await self._seed_positions_from_broker()

        # 4. Run initial reconciliation
        await self._run_reconciliation()

        # 5. Enter main loop
        self.status = RunnerStatus.RUNNING
        self.event_store.log_callback("runner_lifecycle", {
            "action": "started",
            "timestamp": datetime.now(UTC).isoformat(),
        })

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Runner loop cancelled")
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Signal the runner to stop gracefully."""
        logger.info("Stop requested")
        self.status = RunnerStatus.STOPPING
        self._stop_event.set()
        self.event_store.log_callback("runner_lifecycle", {
            "action": "stop_requested",
            "timestamp": datetime.now(UTC).isoformat(),
        })

    def pause(self) -> None:
        """Pause the runner (skip tick processing)."""
        self._pause_event.clear()
        self.status = RunnerStatus.PAUSED
        self.event_store.log_callback("runner_lifecycle", {
            "action": "paused",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        logger.info("Runner paused")

    def resume(self) -> None:
        """Resume the runner from paused state."""
        self._pause_event.set()
        self.status = RunnerStatus.RUNNING
        self.event_store.log_callback("runner_lifecycle", {
            "action": "resumed",
            "timestamp": datetime.now(UTC).isoformat(),
        })
        logger.info("Runner resumed")

    async def _shutdown(self) -> None:
        """Clean shutdown: cancel working orders, disconnect, stop stores."""
        self.status = RunnerStatus.STOPPING
        logger.info("Shutting down runner")

        # Cancel working orders BEFORE disconnecting. Orders left at the
        # broker survive the restart, fill while nobody is watching, and
        # show up as reconciliation mismatches / untracked positions
        # (observed live 2026-06-12: two orphaned entry legs, one filled).
        try:
            active = [
                o for o in self.order_manager.orders.values()
                if o.status in (OrderStatus.NEW, OrderStatus.SUBMITTED,
                                OrderStatus.PARTIALLY_FILLED)
            ]
            for o in active:
                try:
                    await self.order_manager.cancel_order(o.order_id)
                except Exception as e:
                    logger.warning("Shutdown cancel failed for %s: %s", o.order_id, e)
            if active:
                logger.warning("Shutdown: cancelled %d working orders", len(active))
                # Give IBKR a moment to ack so the orders don't outlive us
                for _ in range(50):  # up to 5s
                    if all(o.status not in (OrderStatus.NEW, OrderStatus.SUBMITTED,
                                            OrderStatus.PARTIALLY_FILLED,
                                            OrderStatus.CANCEL_PENDING)
                           for o in active):
                        break
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning("Shutdown order sweep error: %s", e)

        try:
            await self.broker.disconnect()
        except Exception as e:
            logger.warning("Broker disconnect error: %s", e)

        try:
            await self.event_store.stop()
        except Exception as e:
            logger.warning("EventStore stop error: %s", e)

        self.status = RunnerStatus.STOPPED
        logger.info("Runner stopped cleanly")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def _run_reconciliation(self) -> bool:
        """Run reconciliation and return True if clean."""
        self.status = RunnerStatus.RECONCILING
        logger.info("Running reconciliation...")

        # Gather internal open orders
        active_statuses = {
            OrderStatus.NEW, OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
        internal_open_orders = [
            o for o in self.order_manager.orders.values()
            if o.status in active_statuses
        ]

        report = await self.reconciliation_engine.reconcile(internal_open_orders)
        self._last_reconciliation = datetime.now(UTC)

        if report.is_clean:
            logger.info("Reconciliation passed cleanly (matches=%d)", report.matches)
            if self.status == RunnerStatus.RECONCILING:
                self.status = RunnerStatus.RUNNING
            return True
        else:
            logger.warning(
                "Reconciliation failed (matches=%d, mismatches=%d). System locked.",
                report.matches, report.mismatches,
            )
            self.status = RunnerStatus.ERROR
            # Actually engage the lock: entries are gated on
            # override_manager.state.system_locked, and without this the
            # "lock" was a log line only (entries fired 4s after it).
            self.override_manager.state.system_locked = True
            return False

    async def _process_commands(self, now: datetime) -> None:
        """Drain pending control-plane commands through standard paths."""
        try:
            pending = self.command_queue.fetch_pending()
        except Exception as e:
            logger.error("Command queue fetch failed: %s", e)
            return

        for cmd in pending:
            cid, ctype, payload = cmd["command_id"], cmd["type"], cmd["payload"]
            try:
                if ctype == "exit_position":
                    pid = UUID(str(payload["position_id"]))
                    pos = self.position_manager.positions.get(pid)
                    if pos is None or pos.status not in (PositionStatus.OPEN, PositionStatus.OPENING):
                        self.command_queue.mark(cid, "failed", f"position {pid} not open")
                        continue
                    # Same path as a strategy-driven exit (ExitManager)
                    self._manual_exits.add(pid)
                    self.command_queue.mark(cid, "done", "queued for exit via ExitManager")

                elif ctype == "cancel_order":
                    oid = UUID(str(payload["order_id"]))
                    ok = await self.order_manager.cancel_order(oid)
                    self.command_queue.mark(cid, "done" if ok else "failed",
                                            "cancel requested" if ok else "order not active")

                elif ctype == "pause_strategy":
                    sid = str(payload["strategy_id"])
                    if payload.get("paused", True):
                        self.override_manager.pause_strategy(sid)
                        self.command_queue.mark(cid, "done", f"{sid} paused")
                    else:
                        self.override_manager.resume_strategy(sid)
                        self.command_queue.mark(cid, "done", f"{sid} resumed")

                elif ctype == "flatten_all":
                    # Latched until the book is flat; rides ExitManager's
                    # force_flatten path (same as automated force flatten).
                    self._flatten_all_active = True
                    self.command_queue.mark(cid, "done", "flatten_all latched")

                elif ctype == "update_strategy":
                    result = self._apply_strategy_update(
                        str(payload.get("strategy_id", "")),
                        payload.get("changes", {}) or {},
                    )
                    ok = not result.startswith("error")
                    self.command_queue.mark(cid, "done" if ok else "failed", result)

                elif ctype == "unlock_system":
                    # Operator-initiated unlock from the dashboard. Runs a
                    # reconciliation first so the operator unlocks into a
                    # verified-clean state; if reconciliation fails the lock
                    # stays and the result explains why.
                    clean = await self._run_reconciliation()
                    if clean:
                        self.override_manager.unlock_system()
                        self._locked_by_disconnect = False
                        self.status = RunnerStatus.RUNNING
                        logger.warning("System UNLOCKED by operator (reconciliation clean).")
                        self.command_queue.mark(cid, "done", "unlocked (reconciliation clean)")
                    else:
                        self.command_queue.mark(
                            cid, "failed",
                            "reconciliation NOT clean — lock kept; resolve mismatch first "
                            "(or flatten_all and retry)",
                        )

                elif ctype == "lock_system":
                    self.override_manager.lock_system()
                    logger.warning("System LOCKED by operator.")
                    self.command_queue.mark(cid, "done", "locked")

                elif ctype == "restart_runner":
                    logger.warning("RESTART requested by operator; stopping (supervisor will relaunch).")
                    self.command_queue.mark(cid, "done", "restarting")
                    self._stop_task = asyncio.create_task(self.stop())

                elif ctype == "shutdown_runner":
                    logger.warning("SHUTDOWN requested by operator; stopping permanently.")
                    Path("data/.shutdown_requested").touch()
                    self.command_queue.mark(cid, "done", "shutting down")
                    self._stop_task = asyncio.create_task(self.stop())

                else:
                    self.command_queue.mark(cid, "failed", f"unknown type {ctype}")

                self.event_store.log_callback("control_command", {
                    "command_id": cid, "type": ctype, "payload": payload,
                    "timestamp": now.isoformat(),
                })
            except Exception as e:
                logger.error("Command %s (%s) failed: %s", cid, ctype, e)
                try:
                    self.command_queue.mark(cid, "failed", str(e))
                except Exception:
                    pass

    # Dashboard-editable strategy parameters: dotted path -> coercion type.
    # Anything not listed here is rejected.
    EDITABLE_STRATEGY_PARAMS: dict[str, type] = {
        "enabled": bool,
        "exit.stop_loss_pct": float,
        "exit.take_profit_pct": float,
        "exit.time_exit_utc": str,
        "entry.max_contracts": int,
        "entry.entry_time": str,
        "entry.trigger_pct": float,
        "position_sizing_pct": float,
        "leverage": float,
        "allow_reentry": bool,
    }

    def _apply_strategy_update(self, strategy_id: str, changes: dict) -> str:
        """Apply whitelisted config changes live + persist as an overlay.

        Changes take effect on the NEXT entry/exit-rule application; open
        positions keep the exit rules they were given at fill time.
        Persisted to configs/strategy_overrides.yaml (layered over
        strategies.yaml at startup) so the base file keeps its comments.
        """
        cfg = self._strategy_configs.get(strategy_id)
        if cfg is None:
            return f"error: unknown strategy {strategy_id}"

        applied: dict[str, object] = {}
        for key, raw in changes.items():
            expected = self.EDITABLE_STRATEGY_PARAMS.get(key)
            if expected is None:
                return f"error: param {key} is not editable"
            try:
                if expected is bool:
                    val = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
                else:
                    val = expected(raw)
            except (ValueError, TypeError):
                return f"error: bad value {raw!r} for {key}"
            # Apply to the live config object
            target = cfg
            parts = key.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            setattr(target, parts[-1], val)
            applied[key] = val

        if not applied:
            return "error: no changes supplied"

        # Persist overlay
        try:
            overrides_path = self._configs_dir / "strategy_overrides.yaml"
            import yaml as _yaml
            data = {}
            if overrides_path.exists():
                data = _yaml.safe_load(overrides_path.read_text()) or {}
            data.setdefault(strategy_id, {}).update(applied)
            overrides_path.write_text(_yaml.dump(data, default_flow_style=False))
        except Exception as e:
            logger.error("Failed to persist strategy override: %s", e)

        logger.warning("Strategy %s updated from dashboard: %s", strategy_id, applied)
        self.event_store.log_callback("strategy_updated", {
            "strategy_id": strategy_id, "changes": applied,
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return f"applied {applied}"

    def _apply_strategy_overrides_file(self) -> None:
        """Layer configs/strategy_overrides.yaml over loaded strategy configs."""
        overrides_path = self._configs_dir / "strategy_overrides.yaml"
        if not overrides_path.exists():
            return
        try:
            import yaml as _yaml
            data = _yaml.safe_load(overrides_path.read_text()) or {}
        except Exception as e:
            logger.error("Failed to read strategy overrides: %s", e)
            return
        for sid, changes in data.items():
            if sid in self._strategy_configs and isinstance(changes, dict):
                result = []
                cfg = self._strategy_configs[sid]
                for key, val in changes.items():
                    if key not in self.EDITABLE_STRATEGY_PARAMS:
                        continue
                    target = cfg
                    parts = key.split(".")
                    for part in parts[:-1]:
                        target = getattr(target, part)
                    setattr(target, parts[-1], val)
                    result.append(key)
                if result:
                    logger.info("Applied strategy overrides for %s: %s", sid, result)

    def _refresh_traded_today(self, now: datetime) -> None:
        """(Re)load the persisted traded-today set when the NY date changes."""
        from zoneinfo import ZoneInfo
        ny_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if ny_date == self._traded_today_ny_date:
            return
        self._traded_today_ny_date = ny_date
        try:
            self._persisted_traded_today = self.position_store.strategies_traded_on(ny_date)
        except Exception as e:
            logger.error("Failed to load traded-today set: %s", e)
            self._persisted_traded_today = set()
        if self._persisted_traded_today:
            logger.info(
                "Strategies already traded today (%s), entries blocked unless "
                "allow_reentry: %s", ny_date, sorted(self._persisted_traded_today),
            )

    def _entry_window_closed(self, strategy_cfg: StrategyConfig, now: datetime) -> bool:
        """True when 'now' is at/after the strategy's scheduled exit time.

        A strategy whose time exit has passed must never OPEN a position:
        on 2026-06-11 straddles re-entered at 15:46 (after their 15:30
        exit) following a restart, and kept polling all evening. The exit
        time is interpreted in America/New_York, matching _apply_exit_rules.
        """
        time_exit = getattr(strategy_cfg.exit, "time_exit_utc", None)
        if not time_exit:
            return False
        try:
            hh, mm = map(int, str(time_exit).split(":"))
        except (ValueError, TypeError):
            return False
        from zoneinfo import ZoneInfo
        ny = now.astimezone(ZoneInfo("America/New_York"))
        return (ny.hour, ny.minute) >= (hh, mm)

    @property
    def _default_algo(self) -> Optional[str]:
        """Execution algo from broker config (e.g. IBKR Adaptive/Urgent).

        None when the broker has no order_defaults (mock broker) or the
        operator disabled it (adaptive_priority: null in broker.yaml).
        """
        cfg = getattr(self.broker, "config", None)
        defaults = getattr(cfg, "order_defaults", None)
        priority = getattr(defaults, "adaptive_priority", None)
        if priority in ("Urgent", "Normal", "Patient"):
            return f"adaptive_{priority.lower()}"
        return None

    async def _try_auto_resume(self) -> None:
        """Attempt to clear a disconnect-provenance lock after reconnection.

        Sequence: broker reconnected? -> reconcile -> clean? -> unlock and
        resume. Any failure leaves the system locked (fail-closed). Never
        called for manual or reconciliation-mismatch locks.
        """
        try:
            if not await self.broker.is_connected():
                return
            logger.info("Broker reconnected while disconnect-locked; attempting auto-resume...")
            clean = await self._run_reconciliation()
            if not clean:
                logger.error(
                    "Auto-resume blocked: post-reconnect reconciliation is NOT clean. "
                    "System stays locked; manual review required."
                )
                # Lock provenance is no longer 'just a disconnect' — a real
                # mismatch exists, so stop retrying every tick.
                self._locked_by_disconnect = False
                return
            # unlock_system flips state AND persists to overrides.yaml
            self.override_manager.unlock_system()
            self._locked_by_disconnect = False
            self.status = RunnerStatus.RUNNING
            logger.warning("Auto-resume successful: reconciliation clean, system unlocked, resuming.")
            self.event_store.log_callback("system_auto_resume", {
                "timestamp": datetime.now(UTC).isoformat(),
            })
        except Exception as e:
            logger.error("Auto-resume attempt failed: %s", e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Core poll loop: strategies → risk → orders → exits → reconciliation."""
        while not self._stop_event.is_set():
            # Wait if paused
            await self._pause_event.wait()
            if self._stop_event.is_set():
                break

            # PROTECT MODE — a lock NO LONGER freezes the tick loop.
            # While locked: new entries are blocked (gated in _process_tick),
            # but exit management (stops/time exits), dashboard commands
            # (incl. unlock/flatten), reconciliation and state publishing all
            # KEEP RUNNING. A frozen loop abandoned open positions (live
            # incidents 2026-06-10/11); protect mode never does.
            # Disconnect-provenance locks still attempt reconcile-gated
            # auto-resume.
            if self.override_manager.state.system_locked and self._locked_by_disconnect:
                await self._try_auto_resume()

            self._tick_count += 1
            now = datetime.now(UTC)

            try:
                await self._process_tick(now)
            except Exception as e:
                logger.error("Error in tick %d: %s", self._tick_count, e, exc_info=True)
                self.event_store.log_callback("error", {
                    "source": "runner_tick",
                    "tick": self._tick_count,
                    "error": str(e),
                    "timestamp": now.isoformat(),
                })

            # Periodic reconciliation
            if self._last_reconciliation is not None:
                elapsed = (now - self._last_reconciliation).total_seconds()
                if elapsed >= self._reconciliation_interval:
                    await self._run_reconciliation()

            # Sleep between ticks (interruptible by stop)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._tick_interval,
                )
                break  # Stop event was set during sleep
            except asyncio.TimeoutError:
                pass  # Normal: timeout means continue looping

    async def _manage_active_orders(self, now: datetime) -> None:
        """Handle order timeouts, broker rejections, and multi-leg cancellation coordination."""
        # 0. Resolve orders stuck in CANCEL_PENDING (lost confirmations)
        try:
            await self.order_manager.resolve_stuck_cancels()
        except Exception as e:
            logger.error("Stuck-cancel sweep failed: %s", e)

        # 1. Check for broker rejections or spontaneous cancellations (Hard Rule 7 fail-closed)
        if self.order_manager.broker_rejected_order_ids:
            rejected_ids = list(self.order_manager.broker_rejected_order_ids)
            logger.error(
                "Broker rejected/spontaneously cancelled orders detected: %s. Locking system and canceling all active orders.",
                rejected_ids,
            )
            # Lock system via override manager
            self.override_manager.lock_system()

            # Cancel all active orders
            active_statuses = {
                OrderStatus.NEW,
                OrderStatus.RISK_CHECKED,
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            }
            for order_id, order in list(self.order_manager.orders.items()):
                if order.status in active_statuses:
                    logger.info("Canceling active order %s due to system lock", order_id)
                    await self.order_manager.cancel_order(order_id)

            # Log system lock event
            self.event_store.log_callback("system_lock_rejection", {
                "rejected_order_ids": [str(oid) for oid in rejected_ids],
                "timestamp": now.isoformat(),
            })

            # Clear rejected set so we don't repeatedly lock
            self.order_manager.broker_rejected_order_ids.clear()
            return

        # 2. Gather all entry orders
        entry_orders = [
            order for order in self.order_manager.orders.values()
            if order.is_entry
        ]

        # Group entry orders by strategy_id
        entry_by_strat: dict[str, list[OrderState]] = {}
        for order in entry_orders:
            entry_by_strat.setdefault(order.strategy_id, []).append(order)

        # 3. Enforce entry order timeouts (order_timeout_seconds)
        active_entry_statuses = {
            OrderStatus.NEW,
            OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
        }
        for strategy_id, orders in entry_by_strat.items():
            strategy_cfg = self._strategy_configs.get(strategy_id)
            if not strategy_cfg:
                continue

            timeout_sec = strategy_cfg.entry.order_timeout_seconds
            for order in orders:
                if order.status in active_entry_statuses:
                    age = (now - order.created_at).total_seconds()
                    if age >= timeout_sec:
                        logger.info(
                            "Entry order %s for strategy %s timed out (age: %.1fs >= limit: %ds). Canceling.",
                            order.order_id, strategy_id, age, timeout_sec
                        )
                        await self.order_manager.cancel_order(order.order_id)

        # 4+5. Multi-leg coordination over REPLACEMENT CHAINS.
        #
        # The repricer routinely cancels an order and resubmits it at a new
        # price (cancel/replace), linking old.superseded_by = new. A
        # superseded order is NOT a failed leg — its outcome continues in
        # the successor. Treating those routine cancels as leg failures
        # nuked healthy straddle legs and flattened live positions every
        # reprice cycle (live incidents 2026-06-11).
        #
        # A leg has truly FAILED only when the FINAL order of its chain is
        # terminal (CANCELLED/REJECTED/ERROR) with zero fill. Then:
        #   - cancel still-working peer legs (hard cancel — entry abandoned)
        #   - flatten any peer positions already filled (asymmetric)
        # CANCEL_PENDING is in-flight, never a failure signal.
        # Legs correlate ONLY within their submission batch (entry_batch tag).
        # Grouping by calendar day let one failed leg hard-cancel every later
        # cycle of a re-entering strategy for the rest of the day (GTH live
        # test 2026-06-11). Untagged orders (pre-upgrade) fall back to day.
        open_positions = self.position_manager.get_open_positions()
        for strategy_id, orders in entry_by_strat.items():
            orders_by_batch: dict[str, list[OrderState]] = {}
            for order in orders:
                key = order.metadata.get("entry_batch") or f"day:{order.created_at.date()}"
                orders_by_batch.setdefault(key, []).append(order)

            for batch_key, day_orders in orders_by_batch.items():
                # Chain terminals only: orders that were never superseded.
                chain_terminals = [o for o in day_orders if o.superseded_by is None]

                failed_leg = None
                for order in chain_terminals:
                    if (
                        order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.ERROR)
                        and order.filled_quantity == 0
                    ):
                        failed_leg = order
                        break

                if not failed_leg:
                    continue

                # (a) Hard-cancel still-working peer legs of the same entry
                for order in chain_terminals:
                    if order.order_id == failed_leg.order_id:
                        continue
                    if order.status in active_entry_statuses:
                        logger.warning(
                            "Entry leg %s of %s failed terminally (status=%s, filled=0). "
                            "Hard-canceling working peer leg %s.",
                            failed_leg.order_id, strategy_id, failed_leg.status, order.order_id,
                        )
                        await self.order_manager.cancel_order(order.order_id, hard_cancel=True)

                # (b) Flatten peer positions already filled the same day
                #     (the strategy's multi-leg structure is broken).
                for pos in open_positions:
                    if pos.strategy_id != strategy_id:
                        continue
                    pos_entry = self.order_manager.orders.get(pos.entry_order_id)
                    if pos_entry is not None:
                        pos_key = (pos_entry.metadata.get("entry_batch")
                                   or f"day:{pos_entry.created_at.date()}")
                        if pos_key != batch_key:
                            continue
                        # Never flatten because of the position's OWN chain —
                        # a partial fill with cancelled remainder is valid.
                        own_chain = {pos.entry_order_id}
                        o = pos_entry
                        while o is not None and o.superseded_by is not None:
                            own_chain.add(o.superseded_by)
                            o = self.order_manager.orders.get(o.superseded_by)
                        if failed_leg.order_id in own_chain:
                            continue
                    else:
                        # Entry order unknown to OrderManager (seeded position
                        # or unmapped fill id). Fall back to proximity: filled
                        # within 3 minutes of the failed leg's creation AND a
                        # different contract = the broken multi-leg peer; the
                        # same contract is the partial-fill case.
                        entry_t = pos.entry_time or pos.created_at
                        if abs((entry_t - failed_leg.created_at).total_seconds()) > 180:
                            continue
                        fc, pc = failed_leg.contract, pos.contract
                        same_contract = (
                            fc.symbol == pc.symbol and fc.expiry == pc.expiry
                            and float(fc.strike) == float(pc.strike)
                            and str(fc.right) == str(pc.right)
                        )
                        if same_contract:
                            continue
                    if pos.position_id not in self._asymmetric_exits:
                        logger.warning(
                            "Position %s (%s) is asymmetric: peer entry leg %s failed with zero "
                            "fill. Flagging for flattening.",
                            pos.position_id, strategy_id, failed_leg.order_id,
                        )
                        self._asymmetric_exits.add(pos.position_id)

    async def _process_tick(self, now: datetime) -> None:
        """Process a single tick: poll strategies, check exits."""
        logger.debug("Runner tick %d processing start", self._tick_count)

        # A. Check broker connectivity (fail-closed per Hard Rule 7)
        if not await self.broker.is_connected():
            logger.error("Broker client disconnected. Locking system.")
            self.override_manager.state.system_locked = True
            self._locked_by_disconnect = True
            self.event_store.log_callback("system_lock_disconnect", {
                "timestamp": now.isoformat(),
            })
            return

        # A2. Process control-plane commands (dashboard) — same code paths
        # as automated actions, never a parallel route to the broker.
        await self._process_commands(now)

        # B. Manage active orders (handle timeouts, rejections, multi-leg coordination)
        await self._manage_active_orders(now)

        # 1. Poll strategies for entry signals — BLOCKED while locked
        # (protect mode): no new risk, but exits/commands below still run.
        self._refresh_traded_today(now)
        locked = self.override_manager.state.system_locked
        for strategy_id, strategy_cfg in self._strategy_configs.items():
            if locked:
                break
            if not strategy_cfg.enabled:
                continue
            if strategy_id in self.override_manager.state.paused_strategies:
                continue

            # Entry window: never open after the strategy's exit time.
            if self._entry_window_closed(strategy_cfg, now):
                continue

            # One trade per day, restart-proof (position_store backed).
            if (
                not getattr(strategy_cfg, "allow_reentry", False)
                and strategy_id in self._persisted_traded_today
            ):
                continue

            # Back-off after a risk-rejected batch (stops the 1/sec
            # emit->reject->emit loop seen on 2026-06-11).
            retry_after = self._entry_retry_after.get(strategy_id)
            if retry_after is not None and now < retry_after:
                continue

            # Core Rule (AGENTS.md): "always wait until flat"
            # Prevent new entries if strategy has active positions or working orders
            has_open_positions = any(
                p.strategy_id == strategy_id for p in self.position_manager.get_open_positions()
            )
            active_statuses = {
                OrderStatus.NEW, OrderStatus.RISK_CHECKED,
                OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
                OrderStatus.CANCEL_PENDING,
            }
            has_active_orders = any(
                o.strategy_id == strategy_id and o.status in active_statuses
                for o in self.order_manager.orders.values()
            )
            if has_open_positions or has_active_orders:
                continue

            # Isolate per-strategy failures: one broken provider must not
            # abort the whole tick (it previously skipped exit checks and
            # the dashboard snapshot for every strategy that tick).
            try:
                signals = await self._strategy_provider.poll(strategy_cfg, now)
            except Exception as e:
                logger.error("Strategy %s poll failed: %s", strategy_id, e, exc_info=True)
                self.event_store.log_callback("error", {
                    "source": "strategy_poll",
                    "strategy_id": strategy_id,
                    "error": str(e),
                    "timestamp": now.isoformat(),
                })
                continue
            if not signals:
                continue

            # Log all signals generated by strategy
            for signal in signals:
                self.event_store.log_callback("signal", signal)

            # Pre-evaluate risk and quotes for all signals in this batch
            decisions = []
            all_approved = True
            for signal in signals:
                # Build system state snapshot for risk evaluation
                open_positions = self.position_manager.get_open_positions()
                position_infos = [
                    PositionInfo(
                        strategy_id=p.strategy_id,
                        underlying=p.contract.symbol,
                        status=p.status,
                        quantity=p.quantity,
                    )
                    for p in open_positions
                ]

                active_statuses = {
                    OrderStatus.NEW, OrderStatus.RISK_CHECKED,
                    OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.CANCEL_PENDING,
                }
                open_order_count = sum(
                    1 for o in self.order_manager.orders.values()
                    if o.status in active_statuses
                )

                # Calculate daily PnL from closed positions
                daily_pnl = sum(
                    p.realized_pnl for p in self.position_manager.positions.values()
                    if p.status == PositionStatus.CLOSED
                )

                # Get quote for the signal contract
                try:
                    sym_to_query = signal.contract.to_quote_symbol() if hasattr(signal.contract, "to_quote_symbol") else signal.contract.symbol
                    quotes = await self.broker.get_quotes([sym_to_query])
                    quote = quotes.get(sym_to_query)
                except Exception:
                    quote = None

                system_state = SystemState(
                    open_positions=position_infos,
                    open_order_count=open_order_count,
                    daily_pnl=daily_pnl,
                    quote=quote,
                    current_time=now,
                )

                decision = self.risk_engine.evaluate(signal, system_state)
                decisions.append((signal, decision, quote))
                if not decision.approved:
                    all_approved = False

            if all_approved:
                # Submit all signals since the entire batch passed risk check.
                # All legs of this batch share one entry_batch tag so leg
                # coordination never correlates them with other batches.
                entry_batch = uuid4().hex
                for signal, decision, quote in decisions:
                    self.event_store.log_callback("risk_decision", decision)

                    # Build OrderIntent
                    side = OrderSide.BUY if signal.direction == SignalDirection.LONG else OrderSide.SELL
                    limit_price = signal.limit_price
                    price_source = "signal" if limit_price else None
                    if limit_price is None and quote is not None:
                        # LTP-first pricing: use last traded price as initial limit
                        # Falls back to mid, then ask/bid
                        if quote.last is not None and quote.last > 0:
                            limit_price = quote.last
                            price_source = "LTP"
                        elif quote.bid is not None and quote.ask is not None:
                            limit_price = (quote.bid + quote.ask) / 2.0
                            price_source = "mid"
                        elif quote.ask is not None:
                            limit_price = quote.ask
                            price_source = "ask"
                        elif quote.bid is not None:
                            limit_price = quote.bid
                            price_source = "bid"

                    if limit_price is None or limit_price <= 0:
                        logger.warning("No valid limit price for signal %s, skipping", signal.signal_id)
                        continue

                    intent = OrderIntent(
                        signal_id=signal.signal_id,
                        risk_decision_id=decision.risk_decision_id,
                        is_entry=True,
                        strategy_id=signal.strategy_id,
                        contract=signal.contract,
                        side=side,
                        quantity=decision.allowed_quantity,
                        limit_price=limit_price,
                        metadata={**(signal.metadata or {}), "entry_batch": entry_batch},
                    )

                    # Submit order with repricer enabled. First reprice after
                    # 2s (halfway to touch), then at the touch — mirrors the
                    # backtest's fill-at-bar-open assumption as closely as a
                    # limit order can.
                    reprice_cfg = RepriceConfig(
                        enabled=True,
                        max_attempts=6,
                        reprice_interval_seconds=2.0,
                        timeout_seconds=float(strategy_cfg.entry.order_timeout_seconds),
                        # One modify message instead of cancel/replace —
                        # requires plain limits (adaptive_priority: null);
                        # Adaptive orders reject in-place revision.
                        use_in_place_modify=self._default_algo is None,
                    )
                    try:
                        order_state = await self.order_manager.submit_intent(
                            intent, decision, reprice_config=reprice_cfg,
                            algo=self._default_algo,
                        )
                        logger.info(
                            "Order %s submitted for signal %s (status=%s, limit=%.2f, price_source=%s)",
                            order_state.order_id, signal.signal_id, order_state.status,
                            limit_price, price_source,
                        )
                    except Exception as e:
                        logger.error("Order submission failed for signal %s: %s", signal.signal_id, e)
                        self.event_store.log_callback("error", {
                            "source": "order_submission",
                            "signal_id": str(signal.signal_id),
                            "error": str(e),
                            "timestamp": now.isoformat(),
                        })
            else:
                # If one leg failed, reject all signals in the batch to avoid partial/asymmetric exposure
                from datetime import timedelta
                self._entry_retry_after[strategy_id] = now + timedelta(seconds=30)
                for signal, decision, quote in decisions:
                    final_decision = decision
                    if decision.approved:
                        # Override approval status since peer failed
                        final_decision = RiskDecision(
                            signal_id=signal.signal_id,
                            risk_decision_id=decision.risk_decision_id,
                            status=RiskDecisionStatus.REJECTED,
                            allowed_quantity=0,
                            blocking_reasons=["peer_signal_rejected"],
                            warnings=decision.warnings,
                            timestamp=decision.timestamp,
                        )
                    self.event_store.log_callback("risk_decision", final_decision)
                    logger.info(
                        "Signal %s of strategy %s rejected (batch rejected): %s",
                        signal.signal_id, strategy_id, final_decision.blocking_reasons,
                    )

        # 2. Check exits
        try:
            await self._check_exits(now)
        except Exception as e:
            logger.error("Error checking exits: %s", e, exc_info=True)

        # 3. Publish runtime snapshot for the dashboard (DB-only consumer)
        try:
            snapshot = self._create_runtime_snapshot_dict(now)
            await asyncio.to_thread(self.runtime_state_store.write, snapshot)
        except Exception as e:
            logger.error("Error writing runtime snapshot: %s", e)

    def _create_runtime_snapshot_dict(self, now: datetime) -> dict:
        """Create the serialized runner's live view dictionary on the main thread."""
        positions = []
        strategy_pnl: dict[str, dict] = {}
        for pos in self.position_manager.positions.values():
            mult = getattr(pos.contract, "multiplier", 100) or 100
            unreal = None
            if pos.status in (PositionStatus.OPEN, PositionStatus.OPENING) and pos.current_price is not None:
                direction = 1 if pos.side == OrderSide.BUY else -1
                unreal = (pos.current_price - pos.average_entry_price) * pos.quantity * mult * direction
            s = strategy_pnl.setdefault(pos.strategy_id, {"realized": 0.0, "unrealized": 0.0})
            s["realized"] += pos.realized_pnl or 0.0
            if unreal is not None:
                s["unrealized"] += unreal
            if pos.status in (PositionStatus.OPEN, PositionStatus.OPENING):
                positions.append({
                    "position_id": str(pos.position_id),
                    "strategy_id": pos.strategy_id,
                    "contract": f"{pos.contract.symbol} {pos.contract.expiry} {pos.contract.strike} "
                                f"{pos.contract.right.value if hasattr(pos.contract.right, 'value') else pos.contract.right}",
                    "side": pos.side.value,
                    "quantity": pos.quantity,
                    "avg_entry_price": pos.average_entry_price,
                    "current_price": pos.current_price,
                    "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
                    "stop_price": pos.stop_price,
                    "time_exit_utc": pos.time_exit_utc.isoformat() if pos.time_exit_utc else None,
                })

        active_statuses = {
            OrderStatus.NEW, OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
        orders = [{
            "order_id": str(o.order_id),
            "strategy_id": o.strategy_id,
            "is_entry": o.is_entry,
            "contract": f"{o.contract.symbol} {o.contract.strike} "
                        f"{o.contract.right.value if hasattr(o.contract.right, 'value') else o.contract.right}",
            "side": o.side.value,
            "quantity": o.quantity,
            "filled_quantity": o.filled_quantity,
            "limit_price": o.limit_price,
            "status": o.status.value,
            "order_ref": o.order_ref,
        } for o in self.order_manager.orders.values() if o.status in active_statuses]

        strategies = []
        for sid, cfg in self._strategy_configs.items():
            strategies.append({
                "strategy_id": sid,
                "enabled": cfg.enabled,
                "paused": sid in self.override_manager.state.paused_strategies,
                "allow_reentry": bool(getattr(cfg, "allow_reentry", False)),
                "traded_today": sid in self._persisted_traded_today,
                "signal_source": cfg.entry.signal_source,
                "params": {
                    "entry.entry_time": getattr(cfg.entry, "entry_time", None),
                    "entry.trigger_pct": getattr(cfg.entry, "trigger_pct", None),
                    "entry.max_contracts": cfg.entry.max_contracts,
                    "exit.stop_loss_pct": cfg.exit.stop_loss_pct,
                    "exit.take_profit_pct": cfg.exit.take_profit_pct,
                    "exit.time_exit_utc": cfg.exit.time_exit_utc,
                    "position_sizing_pct": cfg.position_sizing_pct,
                    "leverage": cfg.leverage,
                },
            })

        return {
            "timestamp": now.isoformat(),
            "tick": self._tick_count,
            "strategies": strategies,
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "system": {
                "locked": self.override_manager.state.system_locked,
                "reduce_only": self.override_manager.state.reduce_only,
                "paused_strategies": list(self.override_manager.state.paused_strategies),
                "flatten_all_active": self._flatten_all_active,
            },
            "positions": positions,
            "orders": orders,
            "strategy_pnl": {k: {"realized": round(v["realized"], 2),
                                 "unrealized": round(v["unrealized"], 2),
                                 "total": round(v["realized"] + v["unrealized"], 2)}
                             for k, v in strategy_pnl.items()},
        }

    def _positions_with_pending_exit_orders(self) -> set[UUID]:
        """Return set of position_ids that already have an active exit order."""
        active_statuses = {
            OrderStatus.NEW, OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
        pending = set()
        for order in self.order_manager.orders.values():
            if (
                not order.is_entry
                and order.position_id is not None
                and order.status in active_statuses
            ):
                pending.add(order.position_id)
        return pending

    async def _check_exits(self, now: datetime) -> None:
        """Check exit conditions on all open positions."""
        open_positions = self.position_manager.get_open_positions()
        if not open_positions:
            self._asymmetric_exits.clear()
            self._manual_exits.clear()
            self._flatten_all_active = False  # book is flat; latch released
            return

        # Clean up completed exits from the tracking set
        open_pos_ids = {p.position_id for p in open_positions}
        self._asymmetric_exits = self._asymmetric_exits.intersection(open_pos_ids)
        self._manual_exits = self._manual_exits.intersection(open_pos_ids)
        self._exit_attempts = {
            k: v for k, v in self._exit_attempts.items() if k in open_pos_ids
        }

        # Filter out positions that already have a pending exit order (Bug 2 fix)
        pending_exits = self._positions_with_pending_exit_orders()
        eligible_positions = [
            p for p in open_positions if p.position_id not in pending_exits
        ]
        if pending_exits:
            logger.debug(
                "Skipping exit check for %d positions with pending exit orders: %s",
                len(pending_exits), pending_exits,
            )
        if not eligible_positions:
            return

        # Fetch quotes for open positions (prioritize option quote symbols)
        symbols = []
        for p in open_positions:  # fetch quotes for ALL open positions (for price updates)
            if hasattr(p.contract, "to_quote_symbol"):
                symbols.append(p.contract.to_quote_symbol())
            symbols.append(p.contract.symbol)
        symbols = list(set(symbols))
        try:
            quotes = await self.broker.get_quotes(symbols)
        except Exception:
            quotes = {}

        # Update position prices for ALL open positions
        self.position_manager.update_position_prices(quotes)

        # Latest completed 1-min bars per symbol (hybrid stop-loss).
        bars: dict[str, object] = {}
        for sym in symbols:
            try:
                bar = self.broker.get_latest_completed_bar(sym)
            except Exception:
                bar = None
            if bar is not None:
                bars[sym] = bar

        # Strategy-driven exits: providers monitor their own open positions
        # (e.g. 5EMA underlying stops / premium trailing) and flag position
        # ids for the ExitManager strategy_exit path. Per-strategy failures
        # are isolated — a broken provider must not block stop/time exits.
        provider_exits: set[UUID] = set()
        eligible_strategy_ids = {p.strategy_id for p in eligible_positions}
        for strategy_id, strategy_cfg in self._strategy_configs.items():
            if not strategy_cfg.enabled or strategy_id not in eligible_strategy_ids:
                continue
            try:
                flagged = await self._strategy_provider.collect_exits(strategy_cfg, now)
                if flagged:
                    provider_exits |= flagged
            except Exception as e:
                logger.error(
                    "collect_exits failed for %s: %s", strategy_id, e, exc_info=True
                )

        # Check exits only for eligible positions (no pending exit order)
        exit_signals = self.exit_manager.check_exits(
            positions=eligible_positions,
            quotes=quotes,
            current_time=now,
            strategy_exits=self._asymmetric_exits | self._manual_exits | provider_exits,
            force_flatten_all=self._flatten_all_active,
            bars=bars,
            # Exits tolerate 2x the entry spread limit: 0DTE spreads widen
            # late-day and skipping stop evaluation leaves positions
            # unprotected (worse than evaluating against a wide NBBO).
            max_spread_pct=(self._risk_config.spread_limits.max_spread_pct * 2.0)
                if self._risk_config else None,
            # Exits use a wider staleness budget than entries — see
            # QuoteFreshnessConfig.exit_max_age_seconds.
            max_age_seconds=getattr(
                self._risk_config.quote_freshness, "exit_max_age_seconds", 30.0
            ) if self._risk_config else None,
        )

        for pos, exit_intent, trigger_reason in exit_signals:
            # Retry throttle: after the first failed attempt, wait 10s
            # between retries; after 5, alert once a minute and hold.
            attempts, last_at = self._exit_attempts.get(pos.position_id, (0, None))
            if last_at is not None:
                elapsed = (now - last_at).total_seconds()
                if attempts >= 5 and elapsed < 60.0:
                    continue
                if attempts >= 1 and elapsed < 10.0:
                    continue
            if attempts == 5:
                logger.error(
                    "Exit for position %s rejected %d times — possible "
                    "blocking order on the same contract. Retrying once "
                    "per minute; operator action may be required.",
                    pos.position_id, attempts,
                )
            self._exit_attempts[pos.position_id] = (attempts + 1, now)

            logger.info(
                "Exit triggered for position %s: %s",
                pos.position_id, trigger_reason,
            )

            # Build a permissive RiskDecision for exits
            exit_decision = RiskDecision(
                signal_id=exit_intent.signal_id,
                risk_decision_id=exit_intent.risk_decision_id,
                status=RiskDecisionStatus.APPROVED,
                allowed_quantity=exit_intent.quantity,
            )

            # Exits are urgent: chase the touch every 1s; from attempt 3 on,
            # price THROUGH the touch (marketable-plus) so a trending market
            # cannot keep running away from the order. Generous attempt
            # budget, 120s overall timeout.
            exit_reprice_cfg = RepriceConfig(
                enabled=True,
                max_attempts=20,
                reprice_interval_seconds=1.0,
                timeout_seconds=120.0,
                cross_touch_after_attempts=3,
                cross_touch_offset=0.05,
                # Modify in place when on plain limits (no Adaptive) — a
                # 1s chase cadence is impossible via cancel/replace when
                # cancel confirmations take 10-30s.
                use_in_place_modify=self._default_algo is None,
            )
            # IBKR rejects orders when the opposite side of the same US
            # option contract is still working ("Cannot have open orders on
            # both sides"). Cancel any working opposite-side order on this
            # contract before submitting the exit.
            for o in list(self.order_manager.orders.values()):
                if (
                    o.status in (OrderStatus.NEW, OrderStatus.SUBMITTED,
                                 OrderStatus.PARTIALLY_FILLED)
                    and o.contract == pos.contract
                    and o.side != exit_intent.side
                ):
                    logger.warning(
                        "Canceling working %s order %s on %s before exit",
                        o.side.value, o.order_id, pos.contract,
                    )
                    try:
                        await self.order_manager.cancel_order(o.order_id)
                    except Exception as e:
                        logger.warning("Pre-exit cancel failed: %s", e)

            try:
                self._exit_reasons[pos.position_id] = (
                    "manual" if pos.position_id in self._manual_exits else trigger_reason
                )
                order_state = await self.order_manager.submit_intent(
                    exit_intent, exit_decision, reprice_config=exit_reprice_cfg,
                    algo=self._default_algo,
                )
                logger.info(
                    "Exit order %s submitted (trigger=%s, status=%s)",
                    order_state.order_id, trigger_reason, order_state.status,
                )
            except Exception as e:
                logger.error(
                    "Exit order submission failed for position %s: %s",
                    pos.position_id, e,
                )

    # ------------------------------------------------------------------
    # Fill handling integration
    # ------------------------------------------------------------------

    def _apply_exit_rules(self, pos: Position, reference_time: datetime) -> None:
        """Compute and apply stop/target/time-exit rules for a position from
        its strategy's config, anchored at reference_time.

        Used both for normal fill-driven entries and for positions seeded
        from the broker on startup (where there is no FillEvent timestamp).
        """
        strategy_cfg = self._strategy_configs.get(pos.strategy_id)
        if not strategy_cfg or pos.status not in (PositionStatus.OPENING, PositionStatus.OPEN):
            return

        stop_price = None
        target_price = None
        time_exit_utc = None

        # 1. Stop Loss
        if strategy_cfg.exit.stop_loss_pct is not None:
            pct = strategy_cfg.exit.stop_loss_pct
            if pct >= 1.0:
                pct = pct / 100.0
            if pos.side == OrderSide.BUY:
                stop_price = pos.average_entry_price * (1.0 - pct)
            else:
                stop_price = pos.average_entry_price * (1.0 + pct)

        # 2. Take Profit
        if strategy_cfg.exit.take_profit_pct is not None:
            pct = strategy_cfg.exit.take_profit_pct
            if pct >= 1.0:
                pct = pct / 100.0
            if pos.side == OrderSide.BUY:
                target_price = pos.average_entry_price * (1.0 + pct)
            else:
                target_price = pos.average_entry_price * (1.0 - pct)

        # 3a. Relative time exit (max_hold_seconds) — takes precedence
        if getattr(strategy_cfg.exit, "max_hold_seconds", None):
            from datetime import timedelta
            time_exit_utc = reference_time.astimezone(UTC) + timedelta(
                seconds=strategy_cfg.exit.max_hold_seconds
            )
        # 3b. Absolute time exit (America/New_York DST-aware)
        elif strategy_cfg.exit.time_exit_utc is not None:
            try:
                from zoneinfo import ZoneInfo
                hh, mm = map(int, strategy_cfg.exit.time_exit_utc.split(":"))
                tz = ZoneInfo("America/New_York")
                ref_time_ny = reference_time.astimezone(tz)
                exit_time_ny = ref_time_ny.replace(hour=hh, minute=mm, second=0, microsecond=0)
                time_exit_utc = exit_time_ny.astimezone(UTC)
            except Exception as e:
                logger.error("Failed to parse time_exit_utc %s: %s", strategy_cfg.exit.time_exit_utc, e)

        # Set parameters in PositionManager
        if stop_price is not None or target_price is not None or time_exit_utc is not None:
            use_mid = getattr(strategy_cfg.exit, "use_mid_for_exits", False)
            self.position_manager.set_exit_rules(
                position_id=pos.position_id,
                stop_price=stop_price,
                target_price=target_price,
                time_exit_utc=time_exit_utc,
                use_mid_for_exits=use_mid,
            )

    def register_fill_handler(self) -> None:
        """Register a fill callback that routes fills to PositionManager.

        This must be called after _wire_components so that
        PositionManager exists.
        """
        original_callback = self.order_manager._on_broker_fill

        def _fill_and_position(fill_event):
            """Chain: original OrderManager handler → PositionManager."""
            # Align the fill's order_id with OrderManager's OrderState id.
            # The broker layer mints its own UUID per order, so positions
            # built from raw fills referenced an id OrderManager never
            # issued — the asymmetric peer-flatten lookup (entry_order_id
            # -> orders) silently never matched, leaving a filled leg
            # unhedged after its peer failed (GTH live test 2026-06-11).
            meta = getattr(fill_event, "metadata", None) or {}
            om_order = self.order_manager._find_order_by_broker_id(
                str(meta.get("broker_order_id", ""))
            )
            if om_order is not None:
                fill_event.order_id = om_order.order_id

            original_callback(fill_event)
            pos = self.position_manager.handle_fill(fill_event)
            # A fill means this strategy traded today (restart-proof gate)
            self._persisted_traded_today.add(pos.strategy_id)
            self._apply_exit_rules(pos, fill_event.timestamp)
            # Persist attribution so a restart re-seeds the TRUE strategy
            # (and therefore the correct exit rules) for this position.
            async def _persist_bg(p, r):
                try:
                    await asyncio.to_thread(self.position_store.upsert_position, p, close_reason=r)
                except Exception as ex:
                    logger.error("Attribution persist failed for %s: %s", p.position_id, ex)

            try:
                reason = None
                if pos.status == PositionStatus.CLOSED:
                    reason = self._exit_reasons.pop(pos.position_id, None)
                asyncio.get_running_loop().create_task(_persist_bg(pos, reason))
            except Exception as e:
                logger.error("Attribution persist enqueue failed for %s: %s", pos.position_id, e)

        # Replace the broker fill callback
        self.broker._fill_callbacks = [_fill_and_position]

    async def _seed_positions_from_broker(self) -> None:
        """Seed PositionManager from broker positions on startup.

        PositionManager only lives in memory and starts empty on every
        restart. If the broker (paper or live) already holds a position
        from before the restart, it would otherwise become an "orphan":
        ReconciliationEngine reports it as a non-locking
        'broker_only_position' WARN, but ExitManager never learns about
        it, so it is never managed (no stop loss, no time exit) for the
        rest of the session.

        This runs once at startup, before reconciliation. For each broker
        position with no corresponding internal position, we create an
        internal Position (best-effort strategy attribution from
        configs/strategies.yaml by matching underlying symbol against
        enabled strategies) and apply that strategy's exit rules anchored
        to "now" (since the true entry time is unknown).
        """
        try:
            broker_positions = await self.broker.get_positions()
        except Exception as e:
            logger.error("Failed to fetch broker positions for startup seeding: %s", e)
            return

        if not broker_positions:
            return

        now = datetime.now(UTC)
        seeded_contract_keys: set[tuple] = set()

        for bpos in broker_positions:
            # IBKR reports flat (qty 0) rows after a position closes; seeding
            # them created phantom OPEN qty=0 positions in the attribution DB.
            if bpos.quantity == 0:
                continue

            right_str = bpos.contract.right.value if hasattr(bpos.contract.right, "value") else str(bpos.contract.right)
            seeded_contract_keys.add(
                (bpos.contract.symbol, bpos.contract.expiry, float(bpos.contract.strike), right_str)
            )

            # 1. AUTHORITATIVE attribution: persisted position_attribution
            #    row from the previous process (exact contract identity).
            matched_strategy_id = None
            attribution_source = None
            entry_time = now
            attr = self.position_store.find_open_attribution(
                bpos.contract.symbol, bpos.contract.expiry,
                bpos.contract.strike, right_str,
            )
            if attr and attr["strategy_id"] in self._strategy_configs:
                matched_strategy_id = attr["strategy_id"]
                attribution_source = "position_store"
                if attr.get("entry_time"):
                    try:
                        entry_time = datetime.fromisoformat(attr["entry_time"])
                    except (ValueError, TypeError):
                        pass
                # Adopt the persisted position_id so each restart UPDATES the
                # same attribution row instead of inserting a new one (7
                # duplicate OPEN rows accumulated for one contract on
                # 2026-06-11, inflating dashboard history/PnL counts).
                try:
                    bpos.position_id = UUID(str(attr["position_id"]))
                except (ValueError, TypeError):
                    pass

            # 2. Fallback heuristic (logged loudly): first enabled strategy
            #    whose underlying matches this position's symbol.
            if matched_strategy_id is None:
                for strategy_id, cfg in self._strategy_configs.items():
                    if not getattr(cfg, "enabled", False):
                        continue
                    if getattr(cfg, "underlying", None) == bpos.contract.symbol:
                        matched_strategy_id = strategy_id
                        attribution_source = "underlying_heuristic"
                        logger.warning(
                            "No persisted attribution for %s %s %s %s — falling back to "
                            "underlying-match heuristic (strategy=%s). Exit rules may be wrong.",
                            bpos.contract.symbol, bpos.contract.expiry,
                            bpos.contract.strike, right_str, strategy_id,
                        )
                        break

            bpos.strategy_id = matched_strategy_id or "unknown"
            bpos.status = PositionStatus.OPEN
            bpos.entry_time = entry_time
            bpos.created_at = entry_time
            bpos.updated_at = now

            self.position_manager.positions[bpos.position_id] = bpos

            logger.warning(
                "Seeded position from broker on startup: %s %s %s qty=%d avg_price=%.4f -> strategy=%s (source=%s)",
                bpos.contract.symbol, bpos.contract.expiry, bpos.contract.strike,
                bpos.quantity, bpos.average_entry_price, bpos.strategy_id,
                attribution_source,
            )
            self.event_store.log_callback("position_seeded_from_broker", {
                "position_id": bpos.position_id,
                "strategy_id": bpos.strategy_id,
                "attribution_source": attribution_source,
                "contract": f"{bpos.contract.symbol} {bpos.contract.expiry} {bpos.contract.strike} {bpos.contract.right}",
                "side": bpos.side.value,
                "quantity": bpos.quantity,
                "average_entry_price": bpos.average_entry_price,
                "timestamp": now.isoformat(),
            })

            if matched_strategy_id:
                # Anchor exit rules to the TRUE entry time when restored from
                # the store (so time exits fire at the right moment), else now.
                self._apply_exit_rules(bpos, entry_time)
            else:
                logger.error(
                    "Could not attribute seeded position %s %s %s to any enabled strategy; "
                    "no exit rules applied. Manual review required.",
                    bpos.contract.symbol, bpos.contract.expiry, bpos.contract.strike,
                )

            # Persist (or refresh) the seeded position's attribution row
            try:
                self.position_store.upsert_position(bpos)
            except Exception as e:
                logger.error("Attribution persist failed for seeded %s: %s", bpos.position_id, e)

        # Rows for positions the broker no longer holds must not shadow
        # future contract-identity lookups.
        stale = self.position_store.mark_closed_if_absent(seeded_contract_keys)
        if stale:
            logger.info("Attribution store: marked %d stale OPEN rows closed", stale)
