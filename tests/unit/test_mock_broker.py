"""Tests for src.broker.mock_broker — MockBrokerClient.

Covers all 10 simulated scenarios:
1. accepted order
2. rejected order
3. partial fill
4. full fill
5. delayed fill
6. cancel before fill
7. disconnect
8. reconnect
9. stale quote
10. position mismatch
"""

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.broker.interface import BrokerClient
from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.core.enums import OrderSide, OrderStatus, PositionStatus
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


# ===================================================================
# Helpers / Fixtures
# ===================================================================


def _make_contract() -> OptionContract:
    return OptionContract(
        symbol="SPX", expiry="20260520", strike=5200.0, right="CALL"
    )


def _make_order_plan(quantity: int = 5) -> OrderPlan:
    return OrderPlan(
        order_intent_id=uuid4(),
        strategy_id="test_strat",
        contract=_make_contract(),
        side=OrderSide.BUY,
        quantity=quantity,
        limit_price=3.50,
    )


# ===================================================================
# Tests
# ===================================================================


@pytest.mark.anyio
class TestMockBrokerClient:
    async def test_implements_broker_client_interface(self):
        """Verify inheritance and interface implementation."""
        client = MockBrokerClient()
        assert isinstance(client, BrokerClient)

    # 1. Accepted Order
    async def test_accepted_order_flow(self):
        config = MockBrokerConfig(
            acceptance_delay_seconds=0.01,
            auto_fill=False,  # stop at submitted
        )
        client = MockBrokerClient(config)

        order_events = []

        def order_cb(order_state, order_event):
            order_events.append((order_state.status, order_event))

        client.register_order_callback(order_cb)

        plan = _make_order_plan()
        initial_state = await client.place_order(plan)
        assert initial_state.status == OrderStatus.NEW

        # Wait for acceptance simulation
        await asyncio.sleep(0.03)

        assert len(order_events) == 1
        status, event = order_events[0]
        assert status == OrderStatus.SUBMITTED
        assert event.new_status == OrderStatus.SUBMITTED
        assert "accepted" in event.message.lower()

    # 2. Rejected Order
    async def test_rejected_order_flow(self):
        config = MockBrokerConfig(
            acceptance_delay_seconds=0.01,
            reject_probability=1.0,  # 100% rejection
            reject_reason="Insufficient buying power",
        )
        client = MockBrokerClient(config)

        order_events = []

        def order_cb(order_state, order_event):
            order_events.append((order_state.status, order_event))

        client.register_order_callback(order_cb)

        plan = _make_order_plan()
        await client.place_order(plan)

        await asyncio.sleep(0.03)

        # Should only emit REJECTED status callback
        assert len(order_events) == 1
        status, event = order_events[0]
        assert status == OrderStatus.REJECTED
        assert event.new_status == OrderStatus.REJECTED
        assert "Insufficient buying power" in event.message

    # 3. Partial Fill & 4. Full Fill
    async def test_partial_then_full_fill_flow(self):
        config = MockBrokerConfig(
            acceptance_delay_seconds=0.0,
            fill_delay_seconds=0.01,
            partial_fill_qty=2,  # Fill 2 first, then remaining 3
            partial_fill_delay_seconds=0.01,
            auto_fill=True,
        )
        client = MockBrokerClient(config)

        order_events = []
        fill_events = []

        def order_cb(order_state, order_event):
            order_events.append(order_state.status)

        def fill_cb(fill_event):
            fill_events.append(fill_event)

        client.register_order_callback(order_cb)
        client.register_fill_callback(fill_cb)

        plan = _make_order_plan(quantity=5)
        await client.place_order(plan)

        # Await total delay (acceptance=0, fill=0.01, partial=0.01)
        await asyncio.sleep(0.04)

        # Expected status changes: SUBMITTED -> PARTIALLY_FILLED -> FILLED
        assert OrderStatus.SUBMITTED in order_events
        assert OrderStatus.PARTIALLY_FILLED in order_events
        assert OrderStatus.FILLED in order_events

        # 2 fill events should be triggered
        assert len(fill_events) == 2
        assert fill_events[0].filled_quantity == 2
        assert fill_events[1].filled_quantity == 3

        # EventStore stub should record callbacks
        assert len(client.event_store.events) > 0
        order_callbacks = [
            e for e in client.event_store.events if e["type"] == "order_callback"
        ]
        fill_callbacks = [
            e for e in client.event_store.events if e["type"] == "fill_callback"
        ]
        assert len(order_callbacks) >= 3  # SUBMITTED, PARTIALLY_FILLED, FILLED
        assert len(fill_callbacks) == 2

    # 5. Delayed Fill
    async def test_delayed_fill(self):
        config = MockBrokerConfig(
            acceptance_delay_seconds=0.0,
            fill_delay_seconds=0.05,  # 50ms delay
            auto_fill=True,
        )
        client = MockBrokerClient(config)

        order_events = []

        def order_cb(order_state, order_event):
            order_events.append(order_state.status)

        client.register_order_callback(order_cb)

        plan = _make_order_plan()
        await client.place_order(plan)

        # Check immediately: should be NEW or SUBMITTED, not filled yet
        await asyncio.sleep(0.01)
        assert OrderStatus.FILLED not in order_events

        # Wait longer
        await asyncio.sleep(0.06)
        assert OrderStatus.FILLED in order_events

    # 6. Cancel Before Fill
    async def test_cancel_before_fill(self):
        config = MockBrokerConfig(
            acceptance_delay_seconds=0.01,
            fill_delay_seconds=0.05,
            auto_fill=True,
        )
        client = MockBrokerClient(config)

        order_events = []

        def order_cb(order_state, order_event):
            order_events.append(order_state.status)

        client.register_order_callback(order_cb)

        plan = _make_order_plan()
        initial_state = await client.place_order(plan)

        # Cancel immediately
        cancelled = await client.cancel_order(initial_state.broker_order_id)
        assert cancelled is True

        # Let the delays pass
        await asyncio.sleep(0.08)

        # State should be CANCELLED and never transition to FILLED
        assert OrderStatus.CANCELLED in order_events
        assert OrderStatus.FILLED not in order_events

    # 7. Disconnect & 8. Reconnect
    async def test_disconnect_and_reconnect(self):
        client = MockBrokerClient()
        assert await client.is_connected() is True

        # Place order passes when connected
        plan = _make_order_plan()
        await client.place_order(plan)

        # Disconnect
        await client.disconnect()
        assert await client.is_connected() is False

        # Operations should raise ConnectionError when disconnected
        with pytest.raises(ConnectionError, match="disconnected"):
            await client.place_order(plan)

        with pytest.raises(ConnectionError, match="disconnected"):
            await client.cancel_order("any-id")

        with pytest.raises(ConnectionError, match="disconnected"):
            await client.get_positions()

        with pytest.raises(ConnectionError, match="disconnected"):
            await client.get_quotes(["SPX"])

        # Reconnect
        await client.connect()
        assert await client.is_connected() is True

        # Operations should resume
        await client.place_order(plan)

    # 9. Stale Quote Simulation
    async def test_stale_quote_simulation(self):
        # 1. Normal fresh quote
        client = MockBrokerClient()
        quotes = await client.get_quotes(["SPX"])
        assert "SPX" in quotes
        age = (datetime.now(UTC) - quotes["SPX"].timestamp).total_seconds()
        assert age < 1.0

        # 2. Configured staleness of 10 seconds
        config = MockBrokerConfig(stale_quote_age_seconds=10.0)
        client = MockBrokerClient(config)
        quotes = await client.get_quotes(["SPX"])
        age = (datetime.now(UTC) - quotes["SPX"].timestamp).total_seconds()
        assert age >= 9.0

    # 10. Position Mismatch Simulation
    async def test_position_mismatch(self):
        # Default dynamic tracking
        client = MockBrokerClient()
        positions = await client.get_positions()
        assert len(positions) == 0

        # Run a fill to create a dynamic position
        plan = _make_order_plan(quantity=3)
        await client.place_order(plan)
        await asyncio.sleep(0.02)  # wait fill

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 3
        assert positions[0].side == OrderSide.BUY

        # Inject simulated mismatch positions
        contract = _make_contract()
        injected_position = Position(
            strategy_id="test_strat",
            contract=contract,
            side=OrderSide.SELL,
            quantity=10,
            average_entry_price=3.60,
            status=PositionStatus.OPEN,
            entry_order_id=uuid4(),
        )

        config = MockBrokerConfig(simulated_positions=[injected_position])
        mismatch_client = MockBrokerClient(config)

        # get_positions should return injected mismatched position
        positions = await mismatch_client.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity == 10
        assert positions[0].side == OrderSide.SELL

    # Account State check
    async def test_account_state_override(self):
        client = MockBrokerClient()
        state = await client.get_account_state()
        assert isinstance(state, AccountState)
        assert state.account_id == "MOCK_ACC_123"

        custom_state = AccountState(
            account_id="CUSTOM_DU123",
            net_liquidation=250000.0,
            available_funds=200000.0,
            buying_power=800000.0,
            timestamp=datetime.now(UTC),
        )
        config = MockBrokerConfig(simulated_account_state=custom_state)
        client_override = MockBrokerClient(config)
        state_override = await client_override.get_account_state()
        assert state_override.account_id == "CUSTOM_DU123"
        assert state_override.net_liquidation == 250000.0

    # Streaming callback triggers
    async def test_streaming_quote_callbacks(self):
        client = MockBrokerClient()
        quotes_received = []

        def quote_cb(quote):
            quotes_received.append(quote)

        client.register_quote_callback(quote_cb)
        await client.get_quotes(["SPX", "QQQ"])

        assert len(quotes_received) == 2
        assert any(q.symbol == "SPX" for q in quotes_received)
        assert any(q.symbol == "QQQ" for q in quotes_received)


# ===================================================================
# Import / Dependency Checks
# ===================================================================


def test_no_forbidden_imports():
    """Verify that mock_broker does not import ib_async or any strategy modules."""
    import src.broker.mock_broker as mb

    source = inspect.getsource(mb)

    # Check for forbidden module names in import lines
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "ib_async" not in stripped, (
                f"Forbidden ib_async import found in mock_broker.py: {stripped}"
            )
            assert "ib_insync" not in stripped, (
                f"Forbidden ib_insync import found in mock_broker.py: {stripped}"
            )
            assert "strategy" not in stripped.lower(), (
                f"Forbidden strategy import found in mock_broker.py: {stripped}"
            )
