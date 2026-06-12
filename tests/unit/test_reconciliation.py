"""Tests for ReconciliationEngine."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Callable
from uuid import UUID, uuid4

import pytest

from src.broker.interface import BrokerClient
from src.control.overrides import OverrideManager
from src.core.enums import OptionRight, OrderSide, OrderStatus, PositionStatus
from src.core.models import (
    AccountState,
    FillEvent,
    OptionContract,
    OrderEvent,
    OrderPlan,
    OrderState,
    Position,
    QuoteSnapshot,
)
from src.portfolio.position_manager import PositionManager
from src.portfolio.reconciliation import ReconciliationEngine
from src.storage.event_log import EventStore


# ---------------------------------------------------------------------------
# Stub Broker Client for unit tests
# ---------------------------------------------------------------------------

class StubBrokerClient(BrokerClient):
    """Stub BrokerClient returned controlled state for testing reconciliation."""

    def __init__(self) -> None:
        self.positions: list[Position] = []
        self.orders: list[OrderState] = []
        self.connected = True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return self.connected

    async def place_order(self, order_plan: OrderPlan) -> OrderState:
        raise NotImplementedError()

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_positions(self) -> list[Position]:
        return self.positions

    async def get_open_orders(self) -> list[OrderState]:
        return self.orders

    async def get_account_state(self) -> AccountState:
        raise NotImplementedError()

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        return {}

    def register_order_callback(
        self, callback: Callable[[OrderState, OrderEvent], None]
    ) -> None:
        pass

    def register_fill_callback(self, callback: Callable[[FillEvent], None]) -> None:
        pass

    def register_quote_callback(self, callback: Callable[[QuoteSnapshot], None]) -> None:
        pass

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        return 500.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_contract(symbol: str = "SPX", strike: float = 5200.0, right: OptionRight = OptionRight.CALL) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        expiry="20240520",
        strike=strike,
        right=right,
    )


def _make_position(
    strategy_id: str = "strat_a",
    contract: OptionContract | None = None,
    side: OrderSide = OrderSide.BUY,
    qty: int = 5,
    status: PositionStatus = PositionStatus.OPEN,
) -> Position:
    return Position(
        position_id=uuid4(),
        strategy_id=strategy_id,
        contract=contract or _make_contract(),
        side=side,
        quantity=qty,
        average_entry_price=3.00,
        status=status,
        entry_order_id=uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _make_order(
    strategy_id: str = "strat_a",
    contract: OptionContract | None = None,
    side: OrderSide = OrderSide.BUY,
    qty: int = 5,
    broker_order_id: str | None = "broker_order_123",
) -> OrderState:
    return OrderState(
        order_id=uuid4(),
        order_plan_id=uuid4(),
        strategy_id=strategy_id,
        contract=contract or _make_contract(),
        side=side,
        quantity=qty,
        filled_quantity=0,
        limit_price=3.00,
        status=OrderStatus.SUBMITTED,
        broker_order_id=broker_order_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconciliationEngine:
    """Test suite for ReconciliationEngine."""

    @pytest.mark.asyncio
    async def test_clean_reconciliation(self) -> None:
        """Verify clean reconciliation works and doesn't trigger lock."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        contract_a = _make_contract(strike=5200.0)
        contract_b = _make_contract(strike=5300.0)

        # 1. Setup internal open positions
        pos_a = _make_position(strategy_id="strat_a", contract=contract_a, side=OrderSide.BUY, qty=3)
        pos_b = _make_position(strategy_id="strat_b", contract=contract_b, side=OrderSide.SELL, qty=5)
        pos_mgr.positions[pos_a.position_id] = pos_a
        pos_mgr.positions[pos_b.position_id] = pos_b

        # 2. Setup broker positions matching internal
        broker.positions = [
            Position(
                strategy_id="unknown",
                contract=contract_a,
                side=OrderSide.BUY,
                quantity=3,
                average_entry_price=3.00,
                entry_order_id=uuid4(),
            ),
            Position(
                strategy_id="unknown",
                contract=contract_b,
                side=OrderSide.SELL,
                quantity=5,
                average_entry_price=3.00,
                entry_order_id=uuid4(),
            ),
        ]

        # 3. Setup matching orders
        order_1 = _make_order(strategy_id="strat_a", contract=contract_a, qty=2, broker_order_id="b1")
        broker.orders = [
            _make_order(strategy_id="unknown", contract=contract_a, qty=2, broker_order_id="b1"),
        ]

        report = await engine.reconcile([order_1])

        # Verification
        assert report.is_clean is True
        assert report.matches == 3  # 2 positions + 1 order
        assert report.mismatches == 0
        assert len(report.details) == 0
        assert override_mgr.state.system_locked is False

        # Verify event logging
        reconciliation_events = [e for e in store.events if e["type"] == "reconciliation_event"]
        assert len(reconciliation_events) == 1
        assert reconciliation_events[0]["data"]["is_clean"] is True

    @pytest.mark.asyncio
    async def test_position_quantity_mismatch(self) -> None:
        """Verify position quantity or direction mismatch locks the system."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        contract = _make_contract()
        # Internal has long 5 contracts
        pos = _make_position(strategy_id="strat_a", contract=contract, side=OrderSide.BUY, qty=5)
        pos_mgr.positions[pos.position_id] = pos

        # Broker has long 3 contracts (quantity mismatch)
        broker.positions = [
            Position(
                strategy_id="unknown",
                contract=contract,
                side=OrderSide.BUY,
                quantity=3,
                average_entry_price=3.00,
                entry_order_id=uuid4(),
            )
        ]

        report = await engine.reconcile([])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert override_mgr.state.system_locked is True
        assert report.details[0]["type"] == "position_quantity_mismatch"
        assert report.details[0]["internal_quantity"] == 5
        assert report.details[0]["broker_quantity"] == 3

    @pytest.mark.asyncio
    async def test_unknown_broker_position(self) -> None:
        """Verify position existing only at the broker is flagged but does NOT lock system (warn-only)."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        contract = _make_contract()
        # No internal positions, but broker has a position
        broker.positions = [
            Position(
                strategy_id="unknown",
                contract=contract,
                side=OrderSide.BUY,
                quantity=2,
                average_entry_price=3.00,
                entry_order_id=uuid4(),
            )
        ]

        report = await engine.reconcile([])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert "broker_only_position" in [d["type"] for d in report.details]
        assert "SPX 20240520 5200.0 CALL" in report.broker_only
        assert override_mgr.state.system_locked is False  # Warn-only for broker-only positions

    @pytest.mark.asyncio
    async def test_internal_only_position(self) -> None:
        """Verify position existing only internally is flagged and locks system."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        contract = _make_contract()
        # Internal position exists, but broker has none
        pos = _make_position(strategy_id="strat_a", contract=contract, side=OrderSide.BUY, qty=2)
        pos_mgr.positions[pos.position_id] = pos
        broker.positions = []

        report = await engine.reconcile([])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert "internal_only_position" in [d["type"] for d in report.details]
        assert pos.position_id in report.internal_only
        assert override_mgr.state.system_locked is True

    @pytest.mark.asyncio
    async def test_unknown_broker_order(self) -> None:
        """Verify order existing at broker but not internally is flagged (warn-only, no lock)."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        # Broker has an open order, but internal_open_orders is empty
        broker.orders = [
            _make_order(strategy_id="unknown", qty=5, broker_order_id="broker_order_abc"),
        ]

        report = await engine.reconcile([])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert report.details[0]["type"] == "unknown_broker_order"
        assert report.details[0]["broker_order_id"] == "broker_order_abc"
        assert override_mgr.state.system_locked is False  # Warn-only for unknown broker orders

    @pytest.mark.asyncio
    async def test_order_parameter_mismatch(self) -> None:
        """Verify mismatch in order parameters (quantity) is flagged (warn-only, no lock)."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        order_internal = _make_order(qty=5, broker_order_id="b1")
        # Broker order has quantity 3 instead of 5
        order_broker = _make_order(qty=3, broker_order_id="b1")
        broker.orders = [order_broker]

        report = await engine.reconcile([order_internal])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert report.details[0]["type"] == "order_parameter_mismatch"
        assert override_mgr.state.system_locked is False  # Warn-only for order parameter mismatches

    @pytest.mark.asyncio
    async def test_internal_order_not_at_broker(self) -> None:
        """Verify order tracked internally but not found at broker is flagged (warn-only, no lock)."""
        broker = StubBrokerClient()
        store = EventStore()
        pos_mgr = PositionManager(store)
        override_mgr = OverrideManager()
        engine = ReconciliationEngine(broker, pos_mgr, override_mgr, store)

        order_internal = _make_order(broker_order_id="b1")
        broker.orders = []  # Empty at broker

        report = await engine.reconcile([order_internal])

        assert report.is_clean is False
        assert report.mismatches == 1
        assert report.details[0]["type"] == "internal_order_not_at_broker"
        assert override_mgr.state.system_locked is False  # Warn-only for internal-order-not-at-broker
