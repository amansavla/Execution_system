"""Integration tests for IBKRBrokerClient.

Will automatically be skipped if no local TWS or IB Gateway is running
on paper trading ports (7497 or 4002).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Generator
from uuid import uuid4

import pytest

from ib_async import IB

from src.broker.ibkr_broker import IBKRBrokerClient
from src.core.config import BrokerConfig, ConnectionConfig, AccountConfig
from src.core.enums import OptionRight, OrderSide, OrderStatus
from src.core.models import (
    AccountState,
    FillEvent,
    OptionContract,
    OrderEvent,
    OrderPlan,
    OrderState,
    Position,
)
from src.storage.event_log import EventStore

# Standard paper ports
PAPER_PORTS = [7497, 4002]


async def _find_running_paper_port() -> int | None:
    """Scan standard ports to check if TWS/Gateway is listening."""
    for port in PAPER_PORTS:
        ib = IB()
        try:
            # Short timeout to avoid blocking test collections
            await asyncio.wait_for(ib.connectAsync("127.0.0.1", port, clientId=999), timeout=1.5)
            ib.disconnect()
            return port
        except Exception:
            continue
    return None


@pytest.mark.asyncio
async def test_ibkr_paper_lifecycle() -> None:
    """Test entire connection, query, placement, and cancellation lifecycle against paper account."""
    port = await _find_running_paper_port()
    if not port:
        pytest.skip("No running IB Gateway or TWS found on paper trading ports (7497, 4002). Skipping integration test.")

    # 1. Initialize client using real EventStore
    store = EventStore()
    config = BrokerConfig()
    config.connection.port = port
    config.connection.client_id = 997  # Unique diagnostic ID
    config.connection.timeout_seconds = 10
    config.live_trading.enabled = False  # Paper only

    client = IBKRBrokerClient(config, store)

    # 2. Test Connection
    await client.connect()
    assert await client.is_connected() is True

    try:
        # 3. Test Account State Query
        account_state = await client.get_account_state()
        assert isinstance(account_state, AccountState)
        assert account_state.account_id != ""
        assert account_state.net_liquidation >= 0.0

        # 4. Test Positions Query
        positions = await client.get_positions()
        assert isinstance(positions, list)
        for pos in positions:
            assert isinstance(pos, Position)

        # 5. Setup callbacks to check events propagation
        order_events: list[OrderEvent] = []
        order_states: list[OrderState] = []
        fill_events: list[FillEvent] = []

        def order_callback(state: OrderState, event: OrderEvent) -> None:
            order_states.append(state)
            order_events.append(event)

        def fill_callback(event: FillEvent) -> None:
            fill_events.append(event)

        client.register_order_callback(order_callback)
        client.register_fill_callback(fill_callback)

        # 6. Place a safe test limit order (buying SPY Stock at $1.00 so it doesn't execute)
        # Note: even though OptionContractSelector works on options, BrokerClient qualifies and places any Contract
        # Find the next Friday for a valid SPY option contract expiry
        import datetime as dt
        today = datetime.now(UTC).date()
        days_ahead = 4 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        next_friday = today + dt.timedelta(days=days_ahead)
        expiry_str = next_friday.strftime("%Y%m%d")

        contract = OptionContract(
            symbol="SPY",
            expiry=expiry_str,
            strike=550.0,
            right=OptionRight.CALL,
        )

        plan = OrderPlan(
            order_plan_id=uuid4(),
            order_intent_id=uuid4(),
            strategy_id="diagnostic_strat",
            contract=contract,
            side=OrderSide.BUY,
            quantity=1,
            order_type="LMT",
            limit_price=0.01,  # extremely low limit price to avoid executing
            time_in_force="DAY",
        )

        initial_state = await client.place_order(plan)
        assert initial_state.status in (OrderStatus.NEW, OrderStatus.SUBMITTED)
        assert initial_state.broker_order_id is not None

        # Wait briefly for broker status callback to propagate
        for i in range(50):
            if len(order_events) > 0:
                break
            if i > 0 and i % 10 == 0:
                try:
                    await client.ib.reqOpenOrdersAsync()
                except Exception:
                    pass
            await asyncio.sleep(0.1)
        
        # Verify callbacks received status updates
        assert len(order_events) > 0
        assert order_events[-1].new_status == OrderStatus.SUBMITTED

        # Verify EventStore recorded raw callback and model events
        stored_order_callbacks = [e for e in store.events if e["type"] == "order_callback"]
        assert len(stored_order_callbacks) > 0

        stored_order_events = [e for e in store.events if e["type"] == "order_event"]
        assert len(stored_order_events) > 0

        # 7. Cancel the placed order
        # Wait briefly for TWS to fully process the order submission before requesting cancellation
        await asyncio.sleep(1.5)
        cancelled = await client.cancel_order(initial_state.broker_order_id)
        assert cancelled is True

        # Wait for cancellation update to stream in
        for i in range(50):
            if order_events and order_events[-1].new_status in (OrderStatus.CANCELLED, OrderStatus.CANCEL_PENDING):
                break
            if i > 0 and i % 10 == 0:
                try:
                    await client.ib.reqOpenOrdersAsync()
                except Exception:
                    pass
            await asyncio.sleep(0.1)
        assert order_events[-1].new_status in (OrderStatus.CANCELLED, OrderStatus.CANCEL_PENDING)

    finally:
        # Clean up connection
        await client.disconnect()
        assert await client.is_connected() is False
