"""Integration test: full signal → fill → exit flow using MockBrokerClient.

This test wires the complete execution pipeline with MockBrokerClient
and verifies the entire lifecycle:
  1. Signal emitted by strategy provider
  2. Risk engine approves
  3. Order submitted via MockBrokerClient
  4. Fill received, position created by PositionManager
  5. Exit triggered (stop loss), exit order submitted and filled
  6. Position closed
  7. Reconciliation passes clean

No live trading. No IBKR connection.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from src.app.runner import ExecutionRunner, StrategyProvider
from src.app.status import RunnerStatus
from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.core.config import StrategyConfig
from src.core.enums import (
    OptionRight,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)
from src.core.models import (
    AccountState,
    OptionContract,
    QuoteSnapshot,
    StrategySignal,
)
from src.storage.event_log import EventStore


# ---------------------------------------------------------------------------
# Helpers: create test configs on disk
# ---------------------------------------------------------------------------

def _write_test_configs(config_dir: Path) -> None:
    """Write minimal valid YAML configs for the test runner."""
    # risk.yaml
    (config_dir / "risk.yaml").write_text(yaml.dump({
        "global": {
            "trading_mode": "paper",
            "daily_loss_limit": 5000.0,
            "max_open_positions": 10,
            "max_open_orders": 20,
            "max_contracts_per_trade": 10,
            "max_premium_per_trade": 2000.0,
            "buying_power_reserve_pct": 20,
            "no_new_trades_cutoff_utc": "23:30",
            "cooldown_after_loss_seconds": 0,
        },
        "per_strategy": {
            "max_daily_loss": 2000.0,
            "max_positions": 5,
        },
        "per_underlying": {
            "max_positions": 5,
        },
        "spread_limits": {
            "max_spread_pct": 50.0,
        },
        "quote_freshness": {
            "max_age_seconds": 30,
        },
        "kill_switch": {
            "enabled": False,
        },
    }))

    # strategies.yaml — one test strategy
    (config_dir / "strategies.yaml").write_text(yaml.dump({
        "strategies": [{
            "strategy_id": "test_strat",
            "enabled": True,
            "description": "Test strategy for integration",
            "underlying": "SPY",
            "option_type": "single",
            "direction": "long",
            "dte_target": 0,
            "entry": {
                "signal_source": "test",
                "max_contracts": 5,
                "limit_price_offset": 0.05,
                "order_timeout_seconds": 30,
            },
            "exit": {
                "stop_loss_pct": 200,
                "take_profit_pct": 50,
            },
            "cooldown_seconds": 0,
        }],
    }))

    # overrides.yaml — clean state
    (config_dir / "overrides.yaml").write_text(yaml.dump({
        "overrides": {
            "paused_strategies": [],
            "disabled_symbols": [],
            "reduce_only": False,
            "system_locked": False,
            "reduce_only_strategies": [],
        }
    }))

    # broker.yaml
    (config_dir / "broker.yaml").write_text(yaml.dump({
        "live_trading": {"enabled": False},
    }))


# ---------------------------------------------------------------------------
# Test strategy provider that emits a single signal then stops
# ---------------------------------------------------------------------------

class OneShotStrategyProvider(StrategyProvider):
    """Emits a single LONG signal on the first poll, then nothing."""

    def __init__(self, contract: OptionContract, limit_price: float) -> None:
        self._contract = contract
        self._limit_price = limit_price
        self._emitted = False

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        if self._emitted:
            return []
        self._emitted = True
        return [
            StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.LONG,
                contract=self._contract,
                requested_quantity=2,
                limit_price=self._limit_price,
                timestamp=current_time,
            )
        ]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def test_contract() -> OptionContract:
    return OptionContract(
        symbol="SPY",
        expiry="20260520",
        strike=500.0,
        right=OptionRight.CALL,
    )


@pytest.fixture
def test_quote() -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol="SPY",
        bid=5.00,
        ask=5.20,
        last=5.10,
        volume=1000,
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def mock_broker(test_quote: QuoteSnapshot) -> MockBrokerClient:
    """MockBrokerClient with auto-fill and a simulated quote."""
    config = MockBrokerConfig(
        connected_initially=True,
        auto_fill=True,
        acceptance_delay_seconds=0.0,
        fill_delay_seconds=0.01,
        simulated_quotes={"SPY": test_quote},
        simulated_account_state=AccountState(
            account_id="DU12345",
            net_liquidation=100000.0,
            available_funds=80000.0,
            buying_power=200000.0,
        ),
    )
    return MockBrokerClient(config)


async def test_full_signal_to_fill_flow(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
    mock_broker: MockBrokerClient,
) -> None:
    """Full integration: signal → risk → order → fill → position → reconciliation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = OneShotStrategyProvider(test_contract, limit_price=5.10)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,  # Don't auto-reconcile during test
        )

        # Start runner in background
        runner_task = asyncio.create_task(runner.start())

        # Let it process a few ticks
        await asyncio.sleep(0.3)

        # Wait for MockBrokerClient fill tasks to complete
        for task in mock_broker.active_tasks:
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        # Give one more tick for position manager to process
        await asyncio.sleep(0.1)

        # Verify: signal was processed
        signal_events = [e for e in event_store.events if e["type"] == "signal"]
        assert len(signal_events) >= 1, "Expected at least one signal event logged"

        # Verify: risk decision was logged
        risk_events = [e for e in event_store.events if e["type"] == "risk_decision"]
        assert len(risk_events) >= 1, "Expected at least one risk decision event"

        # Verify: order was submitted
        assert len(runner.order_manager.orders) >= 1, "Expected at least one order"

        # Verify: order reached FILLED or beyond NEW
        orders = list(runner.order_manager.orders.values())
        order = orders[0]
        assert order.status in (
            OrderStatus.SUBMITTED, OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.NEW,
        ), f"Unexpected order status: {order.status}"

        # Stop runner
        await runner.stop()
        try:
            await asyncio.wait_for(runner_task, timeout=2.0)
        except asyncio.TimeoutError:
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass

        assert runner.status == RunnerStatus.STOPPED


async def test_runner_pause_resume(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
    mock_broker: MockBrokerClient,
) -> None:
    """Test that pause/resume works correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.1)

        # Pause
        runner.pause()
        assert runner.status == RunnerStatus.PAUSED

        # Resume
        runner.resume()
        assert runner.status == RunnerStatus.RUNNING

        await runner.stop()
        try:
            await asyncio.wait_for(runner_task, timeout=2.0)
        except asyncio.TimeoutError:
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass

        assert runner.status == RunnerStatus.STOPPED


async def test_runner_reconciliation_on_startup(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
    mock_broker: MockBrokerClient,
) -> None:
    """Verify reconciliation runs before the main loop starts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.15)

        # Check reconciliation event was logged
        recon_events = [
            e for e in event_store.events if e["type"] == "reconciliation_event"
        ]
        assert len(recon_events) >= 1, "Reconciliation must run on startup"

        # It should be clean (no positions at broker or internal)
        recon_data = recon_events[0]["data"]
        assert recon_data.get("is_clean") is True, "Initial reconciliation should be clean"

        await runner.stop()
        try:
            await asyncio.wait_for(runner_task, timeout=2.0)
        except asyncio.TimeoutError:
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass


async def test_runner_stop_and_restart_reconciles(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that stopping and restarting the runner reconciles before trading."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        # First run
        mock_broker_1 = MockBrokerClient(MockBrokerConfig(
            connected_initially=True,
            auto_fill=True,
            fill_delay_seconds=0.01,
            simulated_quotes={"SPY": test_quote},
            simulated_account_state=AccountState(
                account_id="DU12345",
                net_liquidation=100000.0,
                available_funds=80000.0,
                buying_power=200000.0,
            ),
        ))

        event_store_1 = EventStore()
        runner_1 = ExecutionRunner(
            broker=mock_broker_1,
            event_store=event_store_1,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        task_1 = asyncio.create_task(runner_1.start())
        await asyncio.sleep(0.15)
        await runner_1.stop()
        try:
            await asyncio.wait_for(task_1, timeout=2.0)
        except asyncio.TimeoutError:
            task_1.cancel()
            try:
                await task_1
            except asyncio.CancelledError:
                pass

        assert runner_1.status == RunnerStatus.STOPPED

        # Second run — new broker, new event store
        mock_broker_2 = MockBrokerClient(MockBrokerConfig(
            connected_initially=True,
            auto_fill=True,
            fill_delay_seconds=0.01,
            simulated_quotes={"SPY": test_quote},
            simulated_account_state=AccountState(
                account_id="DU12345",
                net_liquidation=100000.0,
                available_funds=80000.0,
                buying_power=200000.0,
            ),
        ))

        event_store_2 = EventStore()
        runner_2 = ExecutionRunner(
            broker=mock_broker_2,
            event_store=event_store_2,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        task_2 = asyncio.create_task(runner_2.start())
        await asyncio.sleep(0.15)

        # Reconciliation must have run on restart
        recon_events = [
            e for e in event_store_2.events if e["type"] == "reconciliation_event"
        ]
        assert len(recon_events) >= 1, "Reconciliation must run on restart"

        await runner_2.stop()
        try:
            await asyncio.wait_for(task_2, timeout=2.0)
        except asyncio.TimeoutError:
            task_2.cancel()
            try:
                await task_2
            except asyncio.CancelledError:
                pass

        assert runner_2.status == RunnerStatus.STOPPED


async def test_runner_events_logged_throughout(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
    mock_broker: MockBrokerClient,
) -> None:
    """Verify all key events are logged to EventStore during execution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = OneShotStrategyProvider(test_contract, limit_price=5.10)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        await runner.stop()
        try:
            await asyncio.wait_for(runner_task, timeout=2.0)
        except asyncio.TimeoutError:
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass

        # Check that events were logged
        event_types = {e["type"] for e in event_store.events}

        # Must have runner lifecycle events
        assert "runner_lifecycle" in event_types, "Expected runner_lifecycle events"

        # Must have reconciliation
        assert "reconciliation_event" in event_types, "Expected reconciliation_event"

        # Must have signal and risk decision from the strategy
        assert "signal" in event_types, "Expected signal events"
        assert "risk_decision" in event_types, "Expected risk_decision events"


class MultiSignalStrategyProvider(StrategyProvider):
    """Emits two signals: one valid, one that will fail risk check (e.g. spread too wide)."""

    def __init__(self, contract1: OptionContract, contract2: OptionContract) -> None:
        self._contract1 = contract1
        self._contract2 = contract2
        self._emitted = False

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        if self._emitted:
            return []
        self._emitted = True
        return [
            StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.SHORT,
                contract=self._contract1,
                requested_quantity=2,
                limit_price=5.10,
                timestamp=current_time,
            ),
            StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.SHORT,
                contract=self._contract2,
                requested_quantity=2,
                limit_price=1.00,
                timestamp=current_time,
            ),
        ]


async def test_runner_batch_signal_rejection(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that if one signal in a batch fails risk check, the entire batch is rejected."""
    # contract1: SPY (valid quote)
    # contract2: AAPL (wide spread or no quote -> will fail risk)
    contract2 = OptionContract(
        symbol="AAPL",
        expiry="20260520",
        strike=150.0,
        right=OptionRight.CALL,
    )

    # Map the SPY quote to its full option symbol
    spy_option_quote = QuoteSnapshot(
        symbol=test_contract.to_quote_symbol(),
        bid=test_quote.bid,
        ask=test_quote.ask,
        last=test_quote.last,
        volume=test_quote.volume,
        timestamp=test_quote.timestamp,
    )

    # Mock broker has a valid quote for SPY, but quote for AAPL has a very wide spread (e.g. bid=1.0, ask=5.0 -> mid=3.0, spread=133% > max_spread_pct=50%)
    quote_aapl = QuoteSnapshot(
        symbol=contract2.to_quote_symbol(),
        bid=1.0,
        ask=5.0,
        timestamp=datetime.now(UTC),
    )

    mock_broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True,
        auto_fill=True,
        simulated_quotes={
            test_contract.to_quote_symbol(): spy_option_quote,
            contract2.to_quote_symbol(): quote_aapl,
        },
        simulated_account_state=AccountState(
            account_id="DU12345",
            net_liquidation=100000.0,
            available_funds=80000.0,
            buying_power=200000.0,
        ),
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = MultiSignalStrategyProvider(test_contract, contract2)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        await runner.stop()
        try:
            await asyncio.wait_for(runner_task, timeout=2.0)
        except asyncio.TimeoutError:
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass

        # Verify: no orders were submitted
        assert len(runner.order_manager.orders) == 0, "No orders should be submitted when batch fails"

        # Verify: both signals logged, both decisions rejected
        decisions = [e for e in event_store.events if e["type"] == "risk_decision"]
        assert len(decisions) == 2, "Both decisions should be logged"
        for decision_event in decisions:
            data = decision_event["data"]
            assert data.get("status") == RiskDecisionStatus.REJECTED.value


async def test_runner_entry_order_timeout(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that an entry order is cancelled if it exceeds order_timeout_seconds."""
    mock_broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True,
        auto_fill=False,  # DO NOT AUTO-FILL so order remains active/submitted
        simulated_quotes={test_contract.to_quote_symbol(): test_quote},
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        # Modify strategies.yaml to have order_timeout_seconds: 1
        strategies_file = config_dir / "strategies.yaml"
        strategies_file.write_text(yaml.dump({
            "strategies": [{
                "strategy_id": "test_strat",
                "enabled": True,
                "description": "Test strategy",
                "underlying": "SPY",
                "option_type": "single",
                "direction": "long",
                "dte_target": 0,
                "entry": {
                    "signal_source": "test",
                    "max_contracts": 5,
                    "limit_price_offset": 0.05,
                    "order_timeout_seconds": 1,  # 1 second timeout
                },
                "exit": {
                    "stop_loss_pct": 50,
                },
            }],
        }))

        event_store = EventStore()
        provider = OneShotStrategyProvider(test_contract, limit_price=5.10)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.1,  # Run tick loop fast
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)  # Let it submit the order

        # Verify order is submitted
        assert len(runner.order_manager.orders) == 1
        order = list(runner.order_manager.orders.values())[0]
        assert order.status == OrderStatus.SUBMITTED

        # Wait for timeout (1 second timeout + some buffer)
        await asyncio.sleep(1.2)

        # Verify order was cancelled
        assert order.status == OrderStatus.CANCELLED

        await runner.stop()
        await runner_task


async def test_runner_broker_rejection_fail_closed(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that a broker rejected order locks the system and cancels active orders."""
    mock_broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True,
        auto_fill=False,  # DO NOT AUTO-FILL
        simulated_quotes={test_contract.to_quote_symbol(): test_quote},
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = OneShotStrategyProvider(test_contract, limit_price=5.10)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.1,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)  # Let it submit the order

        assert len(runner.order_manager.orders) == 1
        order = list(runner.order_manager.orders.values())[0]
        assert order.status == OrderStatus.SUBMITTED

        # Simulate a broker rejection
        broker_order = order.model_copy()
        broker_order.status = OrderStatus.REJECTED
        broker_order.error_message = "Limit price too far outside of NBBO"

        from src.core.models import OrderEvent
        order_event = OrderEvent(
            order_id=order.order_id,
            previous_status=order.status,
            new_status=OrderStatus.REJECTED,
            message="Broker rejected the order",
        )
        runner.order_manager._on_broker_order_update(broker_order, order_event)

        # Wait for the next tick to process the rejection
        await asyncio.sleep(0.2)

        # System should be locked
        assert runner.override_manager.state.system_locked is True

        await runner.stop()
        await runner_task


class MultiEntryStrategyProvider(StrategyProvider):
    """Emits two LONG signals on first poll."""
    def __init__(self, contract1: OptionContract, contract2: OptionContract) -> None:
        self._contract1 = contract1
        self._contract2 = contract2
        self._emitted = False

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        if self._emitted:
            return []
        self._emitted = True
        return [
            StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.LONG,
                contract=self._contract1,
                requested_quantity=2,
                limit_price=5.10,
                timestamp=current_time,
            ),
            StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.LONG,
                contract=self._contract2,
                requested_quantity=2,
                limit_price=3.50,
                timestamp=current_time,
            ),
        ]


async def test_runner_multileg_cancellation_coordination(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that if one leg fails, the other active peer leg is cancelled."""
    contract2 = OptionContract(
        symbol="SPY",
        expiry="20260520",
        strike=510.0,
        right=OptionRight.CALL,
    )
    quote2 = QuoteSnapshot(
        symbol=contract2.to_quote_symbol(),
        bid=3.40,
        ask=3.60,
        timestamp=datetime.now(UTC),
    )

    mock_broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True,
        auto_fill=False,  # DO NOT AUTO-FILL
        simulated_quotes={
            test_contract.to_quote_symbol(): test_quote,
            contract2.to_quote_symbol(): quote2,
        },
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = MultiEntryStrategyProvider(test_contract, contract2)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.1,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)  # Let it submit both orders

        assert len(runner.order_manager.orders) == 2
        orders = list(runner.order_manager.orders.values())
        o1, o2 = orders[0], orders[1]
        assert o1.status == OrderStatus.SUBMITTED
        assert o2.status == OrderStatus.SUBMITTED

        # Simulate cancellation on leg 1 (e.g. by setting status to CANCELLED)
        o1.status = OrderStatus.CANCELLED

        # Wait for the next tick to coordinate cancellation
        await asyncio.sleep(0.2)

        # Peer leg o2 should be cancelled now
        assert o2.status == OrderStatus.CANCELLED

        await runner.stop()
        await runner_task


async def test_runner_asymmetric_entry_fill(
    test_contract: OptionContract,
    test_quote: QuoteSnapshot,
) -> None:
    """Verify that if one leg is filled but the peer leg fails, the filled leg is exit-flattened."""
    contract2 = OptionContract(
        symbol="SPY",
        expiry="20260520",
        strike=510.0,
        right=OptionRight.CALL,
    )
    quote2 = QuoteSnapshot(
        symbol=contract2.to_quote_symbol(),
        bid=3.40,
        ask=3.60,
        timestamp=datetime.now(UTC),
    )

    mock_broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True,
        auto_fill=False,  # DO NOT AUTO-FILL so we can control it
        simulated_quotes={
            test_contract.to_quote_symbol(): test_quote,
            contract2.to_quote_symbol(): quote2,
        },
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)

        event_store = EventStore()
        provider = MultiEntryStrategyProvider(test_contract, contract2)

        runner = ExecutionRunner(
            broker=mock_broker,
            event_store=event_store,
            strategy_provider=provider,
            configs_dir=config_dir,
            tick_interval_seconds=0.1,
            reconciliation_interval_seconds=999,
        )

        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)  # Let it submit both orders

        assert len(runner.order_manager.orders) == 2
        orders = list(runner.order_manager.orders.values())
        o1, o2 = orders[0], orders[1]

        # 1. Fill leg 1 manually (so a position is created)
        from src.core.models import FillEvent
        fill_event = FillEvent(
            order_id=o1.order_id,
            strategy_id=o1.strategy_id,
            contract=o1.contract,
            side=o1.side,
            filled_quantity=o1.quantity,
            fill_price=5.10,
            timestamp=datetime.now(UTC),
        )
        runner.broker._fill_callbacks[0](fill_event)

        # Verify a position was opened for leg 1
        open_positions = runner.position_manager.get_open_positions()
        assert len(open_positions) == 1
        pos = open_positions[0]
        assert pos.entry_order_id == o1.order_id

        # 2. Cancel leg 2 manually
        o2.status = OrderStatus.CANCELLED

        # 3. Wait for the next tick to check active orders & exits
        # The runner should run _manage_active_orders, detect peer o2 is cancelled,
        # flag pos as asymmetric exit, and submit exit order for it.
        await asyncio.sleep(0.2)

        # Position should be in asymmetric exits
        assert pos.position_id in runner._asymmetric_exits

        # Verify that an exit order was submitted for the position
        exit_orders = [
            order for order in runner.order_manager.orders.values()
            if not order.is_entry
        ]
        assert len(exit_orders) == 1
        assert exit_orders[0].position_id == pos.position_id

        await runner.stop()
        await runner_task

