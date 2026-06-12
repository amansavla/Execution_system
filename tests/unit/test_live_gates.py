"""Unit tests for Phase 16: Live Mode Safety Gates.

Verifies that live trading requires configurations, environment variables,
and allowlists simultaneously, and checks that safety gates block connection
attempts before they occur.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.broker.ibkr_broker import IBKRBrokerClient
from src.core.config import BrokerConfig
from src.core.enums import OrderSide
from src.core.models import OptionContract, OrderPlan, OrderState
from src.storage.event_log import EventStore


@pytest.fixture
def clean_env():
    """Ensure ALLOW_LIVE_TRADING environment variable is unset for testing."""
    original = os.environ.get("ALLOW_LIVE_TRADING")
    if "ALLOW_LIVE_TRADING" in os.environ:
        del os.environ["ALLOW_LIVE_TRADING"]
    yield
    if original is not None:
        os.environ["ALLOW_LIVE_TRADING"] = original
    elif "ALLOW_LIVE_TRADING" in os.environ:
        del os.environ["ALLOW_LIVE_TRADING"]


@pytest.fixture
def mock_ib():
    """Patch the IB class to prevent actual socket connections."""
    with patch("src.broker.ibkr_broker.IB") as mock_class:
        mock_instance = MagicMock()
        mock_instance.connectAsync = AsyncMock()
        mock_instance.disconnect = MagicMock()
        mock_instance.isConnected = MagicMock(return_value=True)
        mock_instance.managedAccounts = MagicMock(return_value=[])
        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.mark.asyncio
async def test_paper_mode_by_default(clean_env, mock_ib):
    """Scenario 1: live_trading.enabled is False, and session is paper. Should connect normally."""
    config = BrokerConfig()
    config.live_trading.enabled = False
    config.account.account_id = "DU12345"
    config.account.allowlist = []

    mock_ib.managedAccounts.return_value = ["DU12345"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    await client.connect()
    assert mock_ib.connectAsync.called
    assert not mock_ib.disconnect.called


@pytest.mark.asyncio
async def test_live_connection_blocked_when_disabled(clean_env, mock_ib):
    """Scenario 2: live_trading.enabled is False, but session is live. Should disconnect and raise."""
    config = BrokerConfig()
    config.live_trading.enabled = False
    config.account.account_id = "U1234567"
    config.account.allowlist = []

    mock_ib.managedAccounts.return_value = ["U1234567"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(RuntimeError, match="Live connection detected.*Disconnecting immediately"):
        await client.connect()

    assert mock_ib.connectAsync.called
    assert mock_ib.disconnect.called


@pytest.mark.asyncio
async def test_live_mode_missing_env_var(clean_env, mock_ib):
    """Scenario 3: live_trading.enabled is True, but ALLOW_LIVE_TRADING is missing/wrong. Should block before connecting."""
    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U1234567"
    config.account.allowlist = ["U1234567"]

    # Env var is missing
    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING must be set"):
        await client.connect()

    # Verify we blocked connection BEFORE connecting
    assert not mock_ib.connectAsync.called


@pytest.mark.asyncio
async def test_live_mode_incorrect_env_var(clean_env, mock_ib):
    """Scenario 3b: ALLOW_LIVE_TRADING is wrong value. Should block before connecting."""
    os.environ["ALLOW_LIVE_TRADING"] = "NOT_CORRECT_VALUE"

    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U1234567"
    config.account.allowlist = ["U1234567"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING must be set"):
        await client.connect()

    assert not mock_ib.connectAsync.called


@pytest.mark.asyncio
async def test_live_mode_missing_allowlist(clean_env, mock_ib):
    """Scenario 4: ALLOW_LIVE_TRADING is correct, but allowlist is empty. Should block before connecting."""
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U1234567"
    config.account.allowlist = []

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(ValueError, match="account.allowlist must be non-empty"):
        await client.connect()

    assert not mock_ib.connectAsync.called


@pytest.mark.asyncio
async def test_live_mode_mismatched_config_account_id(clean_env, mock_ib):
    """Scenario 5: Configured account ID not in allowlist. Should block before connecting."""
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U9999999"
    config.account.allowlist = ["U1234567"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(ValueError, match="configured account_id 'U9999999' is not present in the account allowlist"):
        await client.connect()

    assert not mock_ib.connectAsync.called


@pytest.mark.asyncio
async def test_live_mode_mismatched_broker_account_id(clean_env, mock_ib):
    """Scenario 6: Actual broker account ID not in allowlist. Should disconnect and raise post-connect."""
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = ""  # auto-detect
    config.account.allowlist = ["U1234567"]

    mock_ib.managedAccounts.return_value = ["U8888888"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    with pytest.raises(ValueError, match="Account ID 'U8888888' is not in allowlist"):
        await client.connect()

    assert mock_ib.connectAsync.called
    assert mock_ib.disconnect.called


@pytest.mark.asyncio
async def test_live_mode_all_gates_pass(clean_env, mock_ib):
    """Scenario 7: All gates pass, connection should succeed."""
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U1234567"
    config.account.allowlist = ["U1234567"]

    mock_ib.managedAccounts.return_value = ["U1234567"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)

    await client.connect()
    assert mock_ib.connectAsync.called
    assert not mock_ib.disconnect.called


@pytest.mark.asyncio
async def test_place_order_safety_gates(clean_env, mock_ib):
    """Scenario 8: Test place_order gate enforcement."""
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    # Setup valid connection first
    config = BrokerConfig()
    config.live_trading.enabled = True
    config.account.account_id = "U1234567"
    config.account.allowlist = ["U1234567"]
    mock_ib.managedAccounts.return_value = ["U1234567"]

    store = EventStore()
    client = IBKRBrokerClient(config, store)
    await client.connect()

    import uuid
    contract = OptionContract(symbol="SPY", expiry="20260520", strike=400.0, right="CALL")
    plan = OrderPlan(
        order_plan_id=uuid.uuid4(),
        order_intent_id=uuid.uuid4(),
        strategy_id="test",
        contract=contract,
        side=OrderSide.BUY,
        quantity=1,
        order_type="LMT",
        limit_price=1.50
    )


    # Mock qualifying contracts and placeOrder
    client._qualify_option_contract = AsyncMock()
    mock_trade = MagicMock()
    mock_trade.order.orderId = 123
    client.ib.placeOrder = MagicMock(return_value=mock_trade)

    # 1. Happy path: live trading works
    res = await client.place_order(plan)
    assert isinstance(res, OrderState)

    # 2. Re-routed to live but env var removed
    del os.environ["ALLOW_LIVE_TRADING"]
    with pytest.raises(ValueError, match="Environment variable ALLOW_LIVE_TRADING must be set"):
        await client.place_order(plan)

    # Restore env var
    os.environ["ALLOW_LIVE_TRADING"] = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"

    # 3. Re-routed to live but account not allowed
    client.config.account.allowlist = ["U9999999"]
    with pytest.raises(ValueError, match="is not in allowlist"):
        await client.place_order(plan)

    # 4. Live session but live_trading disabled in config
    client.config.live_trading.enabled = False
    with pytest.raises(RuntimeError, match="Connected account is LIVE, but live_trading.enabled is False"):
        await client.place_order(plan)
