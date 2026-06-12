import pytest
import asyncio
import tempfile
import yaml
import datetime as dt
from pathlib import Path
from datetime import UTC, date, time, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

from src.app.runner import ExecutionRunner
from src.app.status import RunnerStatus
from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.core.enums import OptionRight, SignalDirection, OrderSide, PositionStatus
from src.core.models import AccountState, OptionContract, QuoteSnapshot
from src.storage.event_log import EventStore
from src.strategies.xsp_breakout import XSPBreakoutStrategyProvider

# Global variable to control the mocked time
mock_now_time = dt.datetime(2026, 5, 21, 14, 1, 0, tzinfo=dt.timezone.utc)

class MockDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is dt.UTC or tz == dt.UTC:
            return mock_now_time
        return mock_now_time.astimezone(tz)


def _write_test_configs(config_dir: Path) -> None:
    """Write minimal YAML configs for XSP Breakout integration test."""
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

    # strategies.yaml — one test strategy matching STRATEGY_PARAMS
    (config_dir / "strategies.yaml").write_text(yaml.dump({
        "strategies": [{
            "strategy_id": "xsp_0dte_1000",
            "enabled": True,
            "description": "XSP 0DTE breakout at 10:00 AM, 0.2% trigger, 30% SL"
            "underlying: XSP",
            "underlying": "XSP",
            "option_type": "single",
            "direction": "long",
            "dte_target": 0,
            "entry": {
                "signal_source": "xsp_breakout",
                "max_contracts": 1,
                "limit_price_offset": 0.05,
                "order_timeout_seconds": 30,
            },
            "exit": {
                "stop_loss_pct": 30,
                "time_exit_utc": "15:30",
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

    # symbols.yaml
    (config_dir / "symbols.yaml").write_text(yaml.dump({
        "symbols": [{
            "symbol": "XSP",
            "exchange": "CBOE",
            "security_type": "IND",
            "option_exchange": "SMART",
            "enabled": True,
            "max_age_seconds": 15,
            "trading_hours_utc": {
                "open": "14:30",
                "close": "21:00",
            },
            "option_multiplier": 100,
        }]
    }))


@pytest.mark.asyncio
async def test_xsp_breakout_integration_flow() -> None:
    global mock_now_time
    tz_ny = ZoneInfo("America/New_York")
    test_date = date(2026, 5, 21)
    
    # 10:01 AM NY time is 14:01 UTC under Daylight Saving Time
    current_time_ny = dt.datetime.combine(test_date, time(10, 1), tzinfo=tz_ny)
    mock_now_time = current_time_ny.astimezone(dt.timezone.utc)
    
    # Option contract that will be selected
    opt_contract = OptionContract(
        symbol="XSP",
        expiry="20260521",
        strike=502.0,
        right=OptionRight.CALL,
    )
    opt_quote_key = opt_contract.to_quote_symbol()
    
    # Mock quote setups (initially fresh)
    underlying_quote = QuoteSnapshot(
        symbol="XSP",
        bid=501.5,
        ask=501.5,
        timestamp=mock_now_time,
    )
    option_quote = QuoteSnapshot(
        symbol=opt_quote_key,
        bid=2.00,
        ask=2.00,
        timestamp=mock_now_time,
    )

    broker_config = MockBrokerConfig(
        connected_initially=True,
        auto_fill=True,
        acceptance_delay_seconds=0.0,
        fill_delay_seconds=0.01,
        simulated_quotes={
            "XSP": underlying_quote,
            opt_quote_key: option_quote,
        },
        simulated_account_state=AccountState(
            account_id="DU12345",
            net_liquidation=100000.0,
            available_funds=80000.0,
            buying_power=200000.0,
        ),
    )
    
    broker = MockBrokerClient(broker_config)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_test_configs(config_dir)
        
        event_store = EventStore()
        provider = XSPBreakoutStrategyProvider(broker)
        # Mock historical 9:30 price at 500.0
        provider.mock_reference_price = 500.0
        
        # Patch datetime inside src.app.runner, src.broker.mock_broker, and src.portfolio.position_manager
        with patch("src.app.runner.datetime", MockDatetime), \
             patch("src.broker.mock_broker.datetime", MockDatetime), \
             patch("src.portfolio.position_manager.datetime", MockDatetime):
            runner = ExecutionRunner(
                broker=broker,
                event_store=event_store,
                strategy_provider=provider,
                configs_dir=config_dir,
                tick_interval_seconds=0.01,
                reconciliation_interval_seconds=999,
            )
            
            # Start runner in background
            runner_task = asyncio.create_task(runner.start())
            
            # Wait a short duration for the loop to run a tick and place order
            await asyncio.sleep(0.3)
            
            # Verify: order was placed and filled
            open_positions = runner.position_manager.get_open_positions()
            assert len(open_positions) == 1, "Should have opened 1 position"
            
            pos = open_positions[0]
            assert pos.strategy_id == "xsp_0dte_1000"
            assert pos.contract.strike == 502.0
            assert pos.contract.right == OptionRight.CALL
            assert pos.average_entry_price == 2.00
            
            # Check exit parameters registered:
            # stop_price = 2.00 * (1 - 0.3) = 1.40
            # time_exit_utc = 15:30 NY time = 19:30 UTC
            assert pos.stop_price == 1.40
            expected_exit_time = dt.datetime.combine(test_date, time(15, 30), tzinfo=tz_ny).astimezone(dt.timezone.utc)
            assert pos.time_exit_utc == expected_exit_time
            
            # Advance time by 1 minute
            mock_now_time += timedelta(minutes=1)
            
            # Trigger exit via stop loss by dropping option price below 1.40 (e.g. bid=1.35)
            # Make sure quote is fresh under new mock time
            option_quote_drop = QuoteSnapshot(
                symbol=opt_quote_key,
                bid=1.35,
                ask=1.35,
                timestamp=mock_now_time,
            )
            broker.config.simulated_quotes[opt_quote_key] = option_quote_drop
            
            # Let another tick process
            await asyncio.sleep(0.2)
            
            # Wait for any background exit fill task
            for task in broker.active_tasks:
                if not task.done():
                    try:
                        await asyncio.wait_for(task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
            await asyncio.sleep(0.1)
            
            # Position should now be CLOSED
            closed_positions = [p for p in runner.position_manager.positions.values() if p.status == PositionStatus.CLOSED]
            assert len(closed_positions) == 1, "Position should be closed via stop loss"
            assert closed_positions[0].realized_pnl is not None
            
            # Verify stop loss limit price matches current bid directly (1.35):
            exit_order_events = [e for e in event_store.events if e["type"] == "order_submitted" and not e["data"].get("is_entry")]
            if not exit_order_events:
                # Let's search inside order manager's submitted orders
                exit_orders = [o for o in runner.order_manager.orders.values() if not o.is_entry]
                assert len(exit_orders) == 1
                assert exit_orders[0].limit_price == 1.35
            else:
                assert exit_order_events[0]["data"]["limit_price"] == 1.35

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
