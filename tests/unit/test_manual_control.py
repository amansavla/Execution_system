"""Tests for ManualControlService, OverrideManager, and CLI.

Covers:
- All 16 manual commands
- Every action logged to EventStore as manual_override
- No direct broker calls (ManualControlService is isolated)
- Reduce-only state propagation
- Flatten routes through PositionManager
- Cancel routes through OrderManager
- CLI parser validates all subcommands
- Override persistence
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4

import pytest

from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.control.manual_control import ManualControlService
from src.control.overrides import OverrideManager
from src.core.config import OverridesConfig
from src.core.enums import (
    OptionRight,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
)
from src.core.models import (
    FillEvent,
    OptionContract,
    OrderIntent,
    Position,
    RiskDecision,
)
from src.execution.order_manager import OrderManager
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_contract(symbol: str = "SPX240520C05200") -> OptionContract:
    return OptionContract(
        symbol=symbol,
        expiry="20240520",
        strike=5200.0,
        right=OptionRight.CALL,
    )


def _make_fill(
    strategy_id: str = "strat_a",
    side: OrderSide = OrderSide.BUY,
    qty: int = 5,
    price: float = 3.00,
) -> FillEvent:
    return FillEvent(
        order_id=uuid4(),
        strategy_id=strategy_id,
        contract=_make_contract(),
        side=side,
        filled_quantity=qty,
        fill_price=price,
    )


def _make_service(
    event_store: Optional[EventStore] = None,
) -> tuple[ManualControlService, EventStore, OrderManager, PositionManager, OverrideManager]:
    """Build a ManualControlService with all dependencies wired."""
    store = event_store or EventStore()
    broker = MockBrokerClient()
    order_mgr = OrderManager(broker, store)
    pos_mgr = PositionManager(store)
    override_mgr = OverrideManager()
    service = ManualControlService(
        event_store=store,
        order_manager=order_mgr,
        position_manager=pos_mgr,
        override_manager=override_mgr,
        operator="test_operator",
    )
    return service, store, order_mgr, pos_mgr, override_mgr


# ---------------------------------------------------------------------------
# OverrideManager tests
# ---------------------------------------------------------------------------

class TestOverrideManager:
    """Test the OverrideManager state mutations."""

    def test_pause_and_resume_strategy(self) -> None:
        mgr = OverrideManager()
        assert mgr.pause_strategy("strat_a") is True
        assert "strat_a" in mgr.state.paused_strategies
        # Idempotent
        assert mgr.pause_strategy("strat_a") is False

        assert mgr.resume_strategy("strat_a") is True
        assert "strat_a" not in mgr.state.paused_strategies
        # Idempotent
        assert mgr.resume_strategy("strat_a") is False

    def test_disable_and_enable_symbol(self) -> None:
        mgr = OverrideManager()
        assert mgr.disable_symbol("SPX") is True
        assert "SPX" in mgr.state.disabled_symbols
        assert mgr.disable_symbol("SPX") is False

        assert mgr.enable_symbol("SPX") is True
        assert "SPX" not in mgr.state.disabled_symbols
        assert mgr.enable_symbol("SPX") is False

    def test_global_reduce_only(self) -> None:
        mgr = OverrideManager()
        assert mgr.set_reduce_only(True) is True
        assert mgr.state.reduce_only is True
        assert mgr.set_reduce_only(True) is False  # idempotent

        assert mgr.set_reduce_only(False) is True
        assert mgr.state.reduce_only is False

    def test_per_strategy_reduce_only(self) -> None:
        mgr = OverrideManager()
        assert mgr.set_reduce_only(True, "strat_a") is True
        assert "strat_a" in mgr.state.reduce_only_strategies
        assert mgr.set_reduce_only(True, "strat_a") is False

        assert mgr.set_reduce_only(False, "strat_a") is True
        assert "strat_a" not in mgr.state.reduce_only_strategies

    def test_lock_and_unlock(self) -> None:
        mgr = OverrideManager()
        assert mgr.lock_system() is True
        assert mgr.state.system_locked is True
        assert mgr.lock_system() is False

        assert mgr.unlock_system() is True
        assert mgr.state.system_locked is False
        assert mgr.unlock_system() is False

    def test_persist_to_yaml(self, tmp_path: Path) -> None:
        """Overrides are persisted to YAML on mutation."""
        yaml_path = tmp_path / "overrides.yaml"
        mgr = OverrideManager(persist_path=yaml_path)
        mgr.pause_strategy("strat_a")
        mgr.disable_symbol("QQQ")
        mgr.lock_system()

        assert yaml_path.exists()
        import yaml

        data = yaml.safe_load(yaml_path.read_text())
        assert "strat_a" in data["overrides"]["paused_strategies"]
        assert "QQQ" in data["overrides"]["disabled_symbols"]
        assert data["overrides"]["system_locked"] is True

    def test_initial_state(self) -> None:
        """OverrideManager respects provided initial state."""
        initial = OverridesConfig(
            paused_strategies=["pre_paused"],
            system_locked=True,
        )
        mgr = OverrideManager(initial_state=initial)
        assert mgr.state.system_locked is True
        assert "pre_paused" in mgr.state.paused_strategies


# ---------------------------------------------------------------------------
# ManualControlService — command tests
# ---------------------------------------------------------------------------

class TestManualControlStatus:
    """Test the status command."""

    def test_status_returns_system_info(self) -> None:
        service, store, _, pos_mgr, override_mgr = _make_service()
        result = service.status()

        assert "system_locked" in result
        assert "reduce_only" in result
        assert "open_positions" in result
        assert "active_orders" in result
        assert result["system_locked"] is False
        assert result["open_positions"] == 0

    def test_status_logs_to_event_store(self) -> None:
        service, store, _, _, _ = _make_service()
        service.status()

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert len(overrides) == 1
        assert overrides[0]["data"]["command"] == "status"


class TestManualControlPauseResume:
    """Test pause/resume strategy commands."""

    def test_pause_strategy(self) -> None:
        service, store, _, _, override_mgr = _make_service()
        result = service.pause_strategy("strat_a")
        assert result == "paused"
        assert "strat_a" in override_mgr.state.paused_strategies

    def test_pause_already_paused(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        service.pause_strategy("strat_a")
        result = service.pause_strategy("strat_a")
        assert result == "already_paused"

    def test_resume_strategy(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        service.pause_strategy("strat_a")
        result = service.resume_strategy("strat_a")
        assert result == "resumed"
        assert "strat_a" not in override_mgr.state.paused_strategies

    def test_resume_not_paused(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.resume_strategy("strat_x")
        assert result == "not_paused"

    def test_pause_logs_manual_override(self) -> None:
        service, store, _, _, _ = _make_service()
        service.pause_strategy("strat_b")

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert len(overrides) == 1
        assert overrides[0]["data"]["command"] == "pause-strategy"
        assert overrides[0]["data"]["target"] == "strat_b"


class TestManualControlSymbols:
    """Test disable/enable symbol commands."""

    def test_disable_symbol(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        result = service.disable_symbol("SPX")
        assert result == "disabled"
        assert "SPX" in override_mgr.state.disabled_symbols

    def test_enable_symbol(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        service.disable_symbol("SPX")
        result = service.enable_symbol("SPX")
        assert result == "enabled"
        assert "SPX" not in override_mgr.state.disabled_symbols

    def test_disable_already_disabled(self) -> None:
        service, _, _, _, _ = _make_service()
        service.disable_symbol("SPX")
        result = service.disable_symbol("SPX")
        assert result == "already_disabled"

    def test_enable_not_disabled(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.enable_symbol("QQQ")
        assert result == "not_disabled"


class TestManualControlReduceOnly:
    """Test reduce-only command."""

    def test_global_reduce_only(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        result = service.reduce_only()
        assert result == "reduce_only_enabled"
        assert override_mgr.state.reduce_only is True

    def test_per_strategy_reduce_only(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        result = service.reduce_only("strat_a")
        assert result == "reduce_only_enabled"
        assert "strat_a" in override_mgr.state.reduce_only_strategies

    def test_reduce_only_already_set(self) -> None:
        service, _, _, _, _ = _make_service()
        service.reduce_only()
        result = service.reduce_only()
        assert result == "already_reduce_only"

    def test_reduce_only_propagates_to_override_state(self) -> None:
        """Verify reduce-only state is readable from override_manager.state."""
        service, _, _, _, override_mgr = _make_service()
        service.reduce_only("strat_x")

        # This is what RiskEngine would read
        assert "strat_x" in override_mgr.state.reduce_only_strategies


class TestManualControlFlatten:
    """Test flatten commands."""

    def test_flatten_position(self) -> None:
        service, _, _, pos_mgr, _ = _make_service()

        # Create a position via fill
        fill = _make_fill(strategy_id="strat_a")
        pos = pos_mgr.handle_fill(fill)
        assert pos.status == PositionStatus.OPEN

        result = service.flatten_position(pos.position_id, 4.00)
        assert result == "flattened"

        # Position should now be closed
        updated = pos_mgr.get_position(pos.position_id)
        assert updated.status == PositionStatus.CLOSED

    def test_flatten_position_not_found(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.flatten_position(uuid4(), 4.00)
        assert result == "position_not_found_or_already_closed"

    def test_flatten_strategy(self) -> None:
        service, _, _, pos_mgr, _ = _make_service()

        # Create 2 positions for strat_a with different contracts
        fill1 = FillEvent(
            order_id=uuid4(),
            strategy_id="strat_a",
            contract=OptionContract(symbol="SPX240520C05200", expiry="20240520", strike=5200.0, right=OptionRight.CALL),
            side=OrderSide.BUY,
            filled_quantity=5,
            fill_price=3.00,
        )
        fill2 = FillEvent(
            order_id=uuid4(),
            strategy_id="strat_a",
            contract=OptionContract(symbol="SPX240520C05300", expiry="20240520", strike=5300.0, right=OptionRight.CALL),
            side=OrderSide.BUY,
            filled_quantity=5,
            fill_price=4.00,
        )
        pos_mgr.handle_fill(fill1)
        pos_mgr.handle_fill(fill2)
        assert len(pos_mgr.get_open_positions()) == 2

        result = service.flatten_strategy("strat_a", 4.00)
        assert "flattened_2" in result
        assert len(pos_mgr.get_open_positions()) == 0

    def test_flatten_strategy_no_positions(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.flatten_strategy("strat_x", 4.00)
        assert result == "no_open_positions"

    def test_flatten_all(self) -> None:
        service, _, _, pos_mgr, _ = _make_service()

        # Create positions for different strategies
        pos_mgr.handle_fill(_make_fill(strategy_id="strat_a"))
        pos_mgr.handle_fill(_make_fill(strategy_id="strat_b"))
        assert len(pos_mgr.get_open_positions()) == 2

        result = service.flatten_all(0.01)
        assert "flattened_2" in result
        assert len(pos_mgr.get_open_positions()) == 0

    def test_flatten_all_no_positions(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.flatten_all(0.01)
        assert result == "no_open_positions"

    def test_flatten_logs_manual_override(self) -> None:
        service, store, _, pos_mgr, _ = _make_service()
        fill = _make_fill()
        pos = pos_mgr.handle_fill(fill)
        service.flatten_position(pos.position_id, 4.00)

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert any(e["data"]["command"] == "flatten-position" for e in overrides)


class TestManualControlCancel:
    """Test cancel commands."""

    @pytest.mark.asyncio
    async def test_cancel_order(self) -> None:
        service, store, order_mgr, _, _ = _make_service()

        # Submit an order to cancel
        intent = OrderIntent(
            signal_id=uuid4(),
            risk_decision_id=uuid4(),
            strategy_id="strat_a",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            limit_price=3.00,
        )
        decision = RiskDecision(
            signal_id=intent.signal_id,
            risk_decision_id=intent.risk_decision_id,
            status=RiskDecisionStatus.APPROVED,
            allowed_quantity=5,
        )
        order_state = await order_mgr.submit_intent(intent, decision)

        # Cancel it
        result = await service.cancel_order(order_state.order_id)
        assert result == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_order_not_found(self) -> None:
        service, _, _, _, _ = _make_service()
        result = await service.cancel_order(uuid4())
        assert result == "cancel_failed_or_not_found"

    @pytest.mark.asyncio
    async def test_cancel_all(self) -> None:
        service, store, order_mgr, _, _ = _make_service()

        # Submit two orders
        for _ in range(2):
            intent = OrderIntent(
                signal_id=uuid4(),
                risk_decision_id=uuid4(),
                strategy_id="strat_a",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=5,
                limit_price=3.00,
            )
            decision = RiskDecision(
                signal_id=intent.signal_id,
                risk_decision_id=intent.risk_decision_id,
                status=RiskDecisionStatus.APPROVED,
                allowed_quantity=5,
            )
            await order_mgr.submit_intent(intent, decision)

        result = await service.cancel_all()
        assert "cancelled" in result

    @pytest.mark.asyncio
    async def test_cancel_all_no_orders(self) -> None:
        service, _, _, _, _ = _make_service()
        result = await service.cancel_all()
        assert result == "no_active_orders"

    @pytest.mark.asyncio
    async def test_cancel_logs_manual_override(self) -> None:
        service, store, order_mgr, _, _ = _make_service()

        intent = OrderIntent(
            signal_id=uuid4(),
            risk_decision_id=uuid4(),
            strategy_id="strat_a",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            limit_price=3.00,
        )
        decision = RiskDecision(
            signal_id=intent.signal_id,
            risk_decision_id=intent.risk_decision_id,
            status=RiskDecisionStatus.APPROVED,
            allowed_quantity=5,
        )
        order_state = await order_mgr.submit_intent(intent, decision)
        await service.cancel_order(order_state.order_id)

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert any(e["data"]["command"] == "cancel-order" for e in overrides)


class TestManualControlLock:
    """Test lock-system command."""

    def test_lock_system(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        result = service.lock_system()
        assert result == "locked"
        assert override_mgr.state.system_locked is True

    def test_lock_already_locked(self) -> None:
        service, _, _, _, _ = _make_service()
        service.lock_system()
        result = service.lock_system()
        assert result == "already_locked"

    def test_lock_logs_manual_override(self) -> None:
        service, store, _, _, _ = _make_service()
        service.lock_system()

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert len(overrides) == 1
        assert overrides[0]["data"]["command"] == "lock-system"


class TestManualControlShowCommands:
    """Test show-* query commands."""

    def test_show_risk(self) -> None:
        service, _, _, _, override_mgr = _make_service()
        override_mgr.pause_strategy("strat_a")
        override_mgr.lock_system()

        result = service.show_risk()
        assert result["system_locked"] is True
        assert "strat_a" in result["paused_strategies"]

    def test_show_positions_empty(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.show_positions()
        assert result == []

    def test_show_positions_with_data(self) -> None:
        service, _, _, pos_mgr, _ = _make_service()
        fill = _make_fill(strategy_id="strat_a")
        pos_mgr.handle_fill(fill)

        result = service.show_positions()
        assert len(result) == 1
        assert result[0]["strategy_id"] == "strat_a"
        assert result[0]["quantity"] == 5

    def test_show_orders_empty(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.show_orders()
        assert result == []

    def test_show_rejections_empty(self) -> None:
        service, _, _, _, _ = _make_service()
        result = service.show_rejections()
        assert result == []

    def test_show_rejections_with_risk_rejection(self) -> None:
        service, store, _, _, _ = _make_service()

        # Simulate a risk rejection event in the store
        store.log_callback("risk_decision", {
            "strategy_id": "strat_a",
            "status": "BLOCKED",
            "blocking_reasons": ["daily_loss_limit_exceeded"],
        })

        result = service.show_rejections()
        assert len(result) == 1
        assert result[0]["type"] == "risk_rejection"
        assert "daily_loss_limit_exceeded" in result[0]["blocking_reasons"]

    def test_show_rejections_with_order_rejection(self) -> None:
        service, store, _, _, _ = _make_service()

        store.log_callback("order_callback", {
            "order_id": str(uuid4()),
            "new_status": "REJECTED",
            "message": "Limit price out of range",
        })

        result = service.show_rejections()
        assert len(result) == 1
        assert result[0]["type"] == "order_rejection"

    def test_show_commands_log_manual_override(self) -> None:
        """All show-* commands log a manual_override event."""
        service, store, _, _, _ = _make_service()
        service.show_risk()
        service.show_positions()
        service.show_orders()
        service.show_rejections()

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        commands = [e["data"]["command"] for e in overrides]
        assert "show-risk" in commands
        assert "show-positions" in commands
        assert "show-orders" in commands
        assert "show-rejections" in commands


# ---------------------------------------------------------------------------
# Every action logged
# ---------------------------------------------------------------------------

class TestAllActionsLogged:
    """Verify every command produces a manual_override event."""

    @pytest.mark.asyncio
    async def test_every_sync_command_logs(self) -> None:
        service, store, _, pos_mgr, _ = _make_service()

        # Execute all sync commands
        service.status()
        service.pause_strategy("s1")
        service.resume_strategy("s1")
        service.disable_symbol("SPX")
        service.enable_symbol("SPX")
        service.reduce_only()
        service.lock_system()
        service.show_risk()
        service.show_positions()
        service.show_orders()
        service.show_rejections()

        # Create position for flatten test
        fill = _make_fill()
        pos = pos_mgr.handle_fill(fill)
        service.flatten_position(pos.position_id, 4.00)
        service.flatten_strategy("strat_x", 4.00)
        service.flatten_all(4.00)

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        # 14 sync commands
        assert len(overrides) == 14

    @pytest.mark.asyncio
    async def test_async_commands_log(self) -> None:
        service, store, _, _, _ = _make_service()

        await service.cancel_order(uuid4())
        await service.cancel_all()

        overrides = [e for e in store.events if e["type"] == "manual_override"]
        assert len(overrides) == 2


# ---------------------------------------------------------------------------
# No direct broker calls
# ---------------------------------------------------------------------------

class TestNoBrokerCalls:
    """Verify ManualControlService has no IBKR or direct broker imports."""

    def test_no_broker_imports_in_manual_control(self) -> None:
        """manual_control.py should not import from src.broker."""
        import inspect
        from src.control import manual_control

        source = inspect.getsource(manual_control)
        assert "from src.broker" not in source
        assert "import ib_async" not in source
        assert "import ibapi" not in source

    def test_no_broker_imports_in_overrides(self) -> None:
        """overrides.py should not import from src.broker."""
        import inspect
        from src.control import overrides

        source = inspect.getsource(overrides)
        assert "from src.broker" not in source
        assert "import ib_async" not in source

    def test_no_broker_imports_in_cli(self) -> None:
        """control.py (CLI) should not import from src.broker."""
        import inspect
        from src.app import control

        source = inspect.getsource(control)
        assert "from src.broker" not in source
        assert "import ib_async" not in source


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------

class TestCLIParser:
    """Test the CLI argument parser structure."""

    def test_parser_recognizes_all_commands(self) -> None:
        from src.app.control import _build_parser

        parser = _build_parser()
        commands = [
            "status",
            "pause-strategy", "resume-strategy",
            "disable-symbol", "enable-symbol",
            "reduce-only",
            "flatten-position", "flatten-strategy", "flatten-all",
            "cancel-order", "cancel-all",
            "lock-system",
            "show-risk", "show-positions", "show-orders", "show-rejections",
        ]
        for cmd in commands:
            # Just verify it parses without error
            if cmd in ("pause-strategy", "resume-strategy"):
                args = parser.parse_args([cmd, "--strategy-id", "test"])
            elif cmd == "disable-symbol":
                args = parser.parse_args([cmd, "--symbol", "SPX"])
            elif cmd == "enable-symbol":
                args = parser.parse_args([cmd, "--symbol", "SPX"])
            elif cmd == "flatten-position":
                args = parser.parse_args([cmd, "--position-id", str(uuid4()), "--exit-price", "1.0"])
            elif cmd == "flatten-strategy":
                args = parser.parse_args([cmd, "--strategy-id", "test", "--exit-price", "1.0"])
            elif cmd == "flatten-all":
                args = parser.parse_args([cmd, "--exit-price", "1.0"])
            elif cmd == "cancel-order":
                args = parser.parse_args([cmd, "--order-id", str(uuid4())])
            else:
                args = parser.parse_args([cmd])

            assert args.command == cmd

    def test_cli_dispatcher_routes_correctly(self) -> None:
        """_run_command routes to correct service methods."""
        from src.app.control import _build_parser, _run_command

        service, _, _, _, _ = _make_service()
        parser = _build_parser()

        # Test sync command dispatch
        args = parser.parse_args(["status"])
        result = asyncio.get_event_loop().run_until_complete(_run_command(service, args))
        assert isinstance(result, dict)
        assert "system_locked" in result

    def test_main_prints_help_on_no_command(self) -> None:
        """main() exits with code 1 when no command is provided."""
        from src.app.control import main

        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
