"""Unit tests for IBKRBrokerClient status mapping and filtering logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from datetime import UTC, datetime

from src.broker.ibkr_broker import IBKRBrokerClient, IB_TO_INTERNAL_STATUS
from src.core.enums import OrderStatus
from src.core.config import BrokerConfig
from src.storage.event_log import EventStore


@pytest.fixture
def mock_ib():
    with patch("src.broker.ibkr_broker.IB") as mock_class:
        mock_instance = MagicMock()
        mock_instance.connectAsync = AsyncMock()
        mock_instance.disconnect = MagicMock()
        mock_instance.isConnected = MagicMock(return_value=True)
        mock_instance.managedAccounts = MagicMock(return_value=["DU12345"])
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.mark.asyncio
async def test_validation_error_status_mapping() -> None:
    """Verify ValidationError IB status maps to OrderStatus.REJECTED."""
    assert IB_TO_INTERNAL_STATUS.get("ValidationError") == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_to_internal_order_state_filled_override(mock_ib) -> None:
    """Verify that remaining=0 and filled>0 forces status to OrderStatus.FILLED."""
    config = BrokerConfig()
    config.live_trading.enabled = False
    config.account.account_id = "DU12345"

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    # Mock trade
    trade = MagicMock()
    trade.order.orderId = 899
    trade.order.action = "BUY"
    trade.order.totalQuantity = 10
    trade.order.lmtPrice = 4.25

    # Mock contract
    contract = MagicMock()
    contract.symbol = "XSP"
    contract.secType = "OPT"
    contract.lastTradeDateOrContractMonth = "20260611"
    contract.strike = 731.0
    contract.right = "P"
    contract.multiplier = "100"
    trade.contract = contract

    # status is ValidationError (which normally maps to REJECTED)
    # but remaining is 0 and filled is 10, so it should be FILLED!
    trade.orderStatus.status = "ValidationError"
    trade.orderStatus.filled = 10
    trade.orderStatus.remaining = 0
    trade.orderStatus.avgFillPrice = 4.25
    trade.orderStatus.lastFillPrice = 4.25

    order_state = client._to_internal_order_state(trade, {})
    assert order_state.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_get_open_orders_filters_active_only(mock_ib) -> None:
    """Verify get_open_orders returns only active open orders."""
    config = BrokerConfig()
    config.live_trading.enabled = False
    config.account.account_id = "DU12345"

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    # Mock 3 trades: 1 active, 1 filled, 1 validation error
    trade_active = MagicMock()
    trade_active.order.orderId = 1
    trade_active.order.action = "BUY"
    trade_active.order.totalQuantity = 5
    trade_active.order.lmtPrice = 2.0
    trade_active.orderStatus.status = "Submitted"
    trade_active.orderStatus.filled = 0
    trade_active.orderStatus.remaining = 5

    trade_filled = MagicMock()
    trade_filled.order.orderId = 2
    trade_filled.order.action = "BUY"
    trade_filled.order.totalQuantity = 5
    trade_filled.order.lmtPrice = 2.0
    trade_filled.orderStatus.status = "Filled"
    trade_filled.orderStatus.filled = 5
    trade_filled.orderStatus.remaining = 0

    trade_err = MagicMock()
    trade_err.order.orderId = 3
    trade_err.order.action = "BUY"
    trade_err.order.totalQuantity = 5
    trade_err.order.lmtPrice = 2.0
    trade_err.orderStatus.status = "ValidationError"
    trade_err.orderStatus.filled = 0
    trade_err.orderStatus.remaining = 5

    for t in (trade_active, trade_filled, trade_err):
        contract = MagicMock()
        contract.symbol = "XSP"
        contract.secType = "OPT"
        contract.lastTradeDateOrContractMonth = "20260611"
        contract.strike = 731.0
        contract.right = "P"
        contract.multiplier = "100"
        t.contract = contract

    mock_ib.openTrades.return_value = [trade_active, trade_filled, trade_err]

    open_orders = await client.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].broker_order_id == "1"
    assert open_orders[0].status == OrderStatus.SUBMITTED
