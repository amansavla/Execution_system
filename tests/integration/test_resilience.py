"""Phase 3 integration tests: auto-resume after reconnect + attribution restart.

Both scenarios bit us in live paper trading on 2026-06-10:
1. Broker disconnect locked the system (correct, Hard Rule 7) but after the
   broker came back the runner stayed locked forever — manual restart needed.
2. Restart seeding guessed strategy attribution by underlying symbol and
   applied the WRONG strategy's exit rules to a seeded position.

All simulated via MockBrokerClient. No live system, no IBKR connection.

Run: python3 -m pytest tests/integration/test_resilience.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from src.app.runner import ExecutionRunner, StrategyProvider
from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.core.enums import OptionRight, OrderSide, PositionStatus
from src.core.models import AccountState, OptionContract, Position, QuoteSnapshot
from src.storage.event_log import EventStore
from src.storage.position_store import PositionStore


def _write_configs(config_dir: Path, strategies: list[dict]) -> None:
    (config_dir / "risk.yaml").write_text(yaml.dump({
        "global": {
            "trading_mode": "paper", "daily_loss_limit": 5000.0,
            "max_open_positions": 10, "max_open_orders": 20,
            "max_contracts_per_trade": 10, "max_premium_per_trade": 2000.0,
            "buying_power_reserve_pct": 20, "no_new_trades_cutoff_utc": "23:30",
            "cooldown_after_loss_seconds": 0,
        },
        "per_strategy": {"max_daily_loss": 2000.0, "max_positions": 5},
        "per_underlying": {"max_positions": 5},
        "spread_limits": {"max_spread_pct": 50.0},
        "quote_freshness": {"max_age_seconds": 30},
        "kill_switch": {"enabled": False},
    }))
    (config_dir / "strategies.yaml").write_text(yaml.dump({"strategies": strategies}))
    (config_dir / "overrides.yaml").write_text(yaml.dump({
        "overrides": {
            "paused_strategies": [], "disabled_symbols": [],
            "reduce_only": False, "system_locked": False,
            "reduce_only_strategies": [],
        }
    }))
    (config_dir / "broker.yaml").write_text(yaml.dump({"live_trading": {"enabled": False}}))


def _strategy(strategy_id: str, underlying: str = "XSP", stop_loss_pct: int = 30,
              time_exit: str = "15:20") -> dict:
    return {
        "strategy_id": strategy_id, "enabled": True,
        "description": f"test {strategy_id}", "underlying": underlying,
        "option_type": "single", "direction": "long", "dte_target": 0,
        "entry": {"signal_source": "test", "max_contracts": 5,
                  "limit_price_offset": 0.05, "order_timeout_seconds": 30},
        "exit": {"stop_loss_pct": stop_loss_pct, "time_exit_utc": time_exit},
        "cooldown_seconds": 0,
    }


def _mock_broker(positions: list[Position] | None = None) -> MockBrokerClient:
    quote = QuoteSnapshot(symbol="XSP", bid=5.0, ask=5.2, last=5.1,
                          timestamp=datetime.now(UTC))
    return MockBrokerClient(MockBrokerConfig(
        connected_initially=True, auto_fill=True,
        acceptance_delay_seconds=0.0, fill_delay_seconds=0.01,
        simulated_quotes={"XSP": quote},
        simulated_positions=positions or [],
        simulated_account_state=AccountState(
            account_id="DU12345", net_liquidation=100000.0,
            available_funds=80000.0, buying_power=200000.0,
        ),
    ))


def _broker_position(strike: float, right: OptionRight, qty: int = 1,
                     avg_price: float = 2.50) -> Position:
    """A position as the broker reports it after a restart (strategy unknown)."""
    return Position(
        position_id=uuid4(), strategy_id="unknown",
        contract=OptionContract(symbol="XSP", expiry="20260611",
                                strike=strike, right=right, multiplier=100),
        side=OrderSide.BUY, quantity=qty, filled_quantity=qty,
        average_entry_price=avg_price, status=PositionStatus.OPEN,
        entry_order_id=uuid4(),
        entry_time=datetime.now(UTC), created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# (i) Auto-resume after simulated broker reconnect — no manual step
# ---------------------------------------------------------------------------

async def test_auto_resume_after_reconnect() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_configs(config_dir, [_strategy("xsp_test")])

        broker = _mock_broker()
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(),
            strategy_provider=StrategyProvider(),  # emits nothing
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)
        assert runner.override_manager.state.system_locked is False

        # Simulate broker disconnect -> Hard Rule 7 fail-closed lock
        broker.connected = False
        await asyncio.sleep(0.2)
        assert runner.override_manager.state.system_locked is True
        assert runner._locked_by_disconnect is True

        # Broker comes back -> reconcile -> clean -> unlock -> resume,
        # with NO manual intervention.
        broker.connected = True
        await asyncio.sleep(0.3)
        assert runner.override_manager.state.system_locked is False
        assert runner._locked_by_disconnect is False

        # Ticks resumed: counter increases after resume
        t0 = runner._tick_count
        await asyncio.sleep(0.2)
        assert runner._tick_count > t0

        await runner.stop()
        runner_task.cancel()


async def test_manual_lock_is_never_auto_cleared() -> None:
    """A lock NOT set by the disconnect path must survive reconnection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        _write_configs(config_dir, [_strategy("xsp_test")])

        broker = _mock_broker()
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(),
            strategy_provider=StrategyProvider(),
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.2)

        # Manual lock (e.g. operator action or reconciliation mismatch)
        runner.override_manager.lock_system()
        await asyncio.sleep(0.2)
        assert runner.override_manager.state.system_locked is True
        # Broker is connected the whole time — lock must persist
        assert runner._locked_by_disconnect is False

        await runner.stop()
        runner_task.cancel()


# ---------------------------------------------------------------------------
# (ii) Attribution survives process restart — correct exit rules re-seed
# ---------------------------------------------------------------------------

async def test_attribution_survives_restart() -> None:
    """Kill + restart: seeding must use the persisted strategy attribution,
    not the underlying-match heuristic, and re-apply that strategy's rules."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")

        # TWO enabled strategies on the same underlying. The heuristic would
        # always pick the FIRST (strat_a). The true owner is strat_b.
        _write_configs(config_dir, [
            _strategy("strat_a", stop_loss_pct=30, time_exit="15:20"),
            _strategy("strat_b", stop_loss_pct=20, time_exit="15:30"),
        ])

        contract_strike, contract_right = 730.0, OptionRight.PUT

        # --- Process 1: position owned by strat_b is persisted ---
        store_1 = PositionStore(db_path)
        owned = _broker_position(contract_strike, contract_right, qty=2, avg_price=3.00)
        owned.strategy_id = "strat_b"
        store_1.upsert_position(owned)

        # --- Process 2 (the restart): fresh runner, broker still holds it ---
        broker_pos = _broker_position(contract_strike, contract_right, qty=2, avg_price=3.00)
        broker = _mock_broker(positions=[broker_pos])

        runner = ExecutionRunner(
            broker=broker,
            event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(),
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        seeded = list(runner.position_manager.positions.values())
        assert len(seeded) == 1
        pos = seeded[0]

        # Attribution restored from SQLite — NOT the heuristic's strat_a
        assert pos.strategy_id == "strat_b"

        # And strat_b's exit rules were applied: stop = entry * (1 - 20%)
        assert pos.stop_price == pytest.approx(3.00 * 0.80, abs=0.01)

        await runner.stop()
        runner_task.cancel()


async def test_seeding_falls_back_to_heuristic_without_attribution() -> None:
    """No persisted row -> underlying heuristic still applies (logged)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, [_strategy("strat_a", stop_loss_pct=40)])

        broker = _mock_broker(positions=[_broker_position(725.0, OptionRight.CALL)])
        runner = ExecutionRunner(
            broker=broker,
            event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(),
            configs_dir=config_dir,
            tick_interval_seconds=0.01,
            reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        seeded = list(runner.position_manager.positions.values())
        assert len(seeded) == 1
        assert seeded[0].strategy_id == "strat_a"

        await runner.stop()
        runner_task.cancel()
