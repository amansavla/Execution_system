"""Phase 7 integration tests: multi-strategy attribution under
normal operation, after reconnect, and after restart.

Run: python3 -m pytest tests/integration/test_multi_strategy.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.app.runner import ExecutionRunner, StrategyProvider
from src.core.config import StrategyConfig
from src.core.enums import OptionRight, OrderSide, SignalDirection
from src.core.models import OptionContract, StrategySignal
from src.storage.event_log import EventStore
from src.storage.position_store import PositionStore
from src.strategies.composite import CompositeStrategyProvider

from tests.integration.test_resilience import (
    _broker_position,
    _mock_broker,
    _strategy,
    _write_configs,
)


class OneShotProvider(StrategyProvider):
    """Emits one signal for its strategy, then nothing (per strategy_id)."""

    def __init__(self, contract: OptionContract, direction: SignalDirection,
                 limit_price: float = 2.50) -> None:
        self._contract = contract
        self._direction = direction
        self._limit = limit_price
        self._emitted: set[str] = set()

    async def poll(self, strategy_config: StrategyConfig,
                   current_time: datetime) -> list[StrategySignal]:
        if strategy_config.strategy_id in self._emitted:
            return []
        self._emitted.add(strategy_config.strategy_id)
        return [StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=self._direction,
            contract=self._contract,
            requested_quantity=1,
            limit_price=self._limit,
            timestamp=current_time,
        )]


def _two_strategy_configs() -> list[dict]:
    a = _strategy("strat_breakout", stop_loss_pct=30, time_exit="23:59")
    a["entry"]["signal_source"] = "source_a"
    b = _strategy("strat_straddle", stop_loss_pct=20, time_exit="23:58")
    b["entry"]["signal_source"] = "source_b"
    return [a, b]


def _contract(strike: float, right: OptionRight) -> OptionContract:
    return OptionContract(symbol="XSP", expiry="20990101", strike=strike,
                          right=right, multiplier=100)


async def test_simultaneous_strategies_attribute_correctly() -> None:
    """Normal operation: two strategies on the SAME underlying fill
    concurrently; each position carries its own strategy_id, exit rules,
    and persisted attribution row."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, _two_strategy_configs())

        broker = _mock_broker()
        # quotes for the two option contracts so risk/exits have data
        c_a = _contract(730.0, OptionRight.CALL)
        c_b = _contract(725.0, OptionRight.PUT)
        from src.core.models import QuoteSnapshot
        for c in (c_a, c_b):
            broker.config.simulated_quotes[c.to_quote_symbol()] = QuoteSnapshot(
                symbol=c.to_quote_symbol(), bid=2.40, ask=2.60, last=2.50,
                timestamp=datetime.now(UTC))

        provider = CompositeStrategyProvider({
            "source_a": OneShotProvider(c_a, SignalDirection.LONG),
            "source_b": OneShotProvider(c_b, SignalDirection.SHORT),
        })

        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=provider, configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.5)

        positions = list(runner.position_manager.positions.values())
        by_strategy = {p.strategy_id: p for p in positions}
        assert set(by_strategy) == {"strat_breakout", "strat_straddle"}

        # Exit rules follow each strategy's own config
        pa, pb = by_strategy["strat_breakout"], by_strategy["strat_straddle"]
        assert pa.stop_price == pytest.approx(pa.average_entry_price * 0.70, abs=0.01)
        # short position: stop ABOVE entry
        assert pb.stop_price == pytest.approx(pb.average_entry_price * 1.20, abs=0.01)

        # Attribution rows persisted for both
        store = PositionStore(db_path)
        attr_a = store.find_open_attribution("XSP", "20990101", 730.0, "CALL")
        attr_b = store.find_open_attribution("XSP", "20990101", 725.0, "PUT")
        assert attr_a and attr_a["strategy_id"] == "strat_breakout"
        assert attr_b and attr_b["strategy_id"] == "strat_straddle"

        await runner.stop()
        runner_task.cancel()


async def test_attribution_survives_reconnect_with_two_strategies() -> None:
    """Disconnect/reconnect (auto-resume) must not disturb attribution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, _two_strategy_configs())

        # Pre-existing broker positions (so we skip the fill plumbing) with
        # persisted attribution for BOTH strategies on the same underlying.
        pos_a = _broker_position(730.0, OptionRight.CALL, qty=1, avg_price=2.50)
        pos_b = _broker_position(725.0, OptionRight.PUT, qty=1, avg_price=0.40)
        store = PositionStore(db_path)
        pos_a.strategy_id = "strat_breakout"
        pos_b.strategy_id = "strat_straddle"
        pos_b.side = OrderSide.SELL  # short leg
        store.upsert_position(pos_a)
        store.upsert_position(pos_b)

        broker = _mock_broker(positions=[pos_a, pos_b])
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(), configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        def snapshot() -> dict[str, str]:
            return {f"{p.contract.strike}{p.contract.right}": p.strategy_id
                    for p in runner.position_manager.positions.values()}

        before = snapshot()
        assert set(before.values()) == {"strat_breakout", "strat_straddle"}

        # Simulated disconnect -> lock -> reconnect -> auto-resume
        broker.connected = False
        await asyncio.sleep(0.2)
        assert runner.override_manager.state.system_locked is True
        broker.connected = True
        await asyncio.sleep(0.4)
        assert runner.override_manager.state.system_locked is False

        assert snapshot() == before  # attribution untouched by the cycle

        await runner.stop()
        runner_task.cancel()


async def test_attribution_survives_restart_with_two_strategies() -> None:
    """Restart with BOTH strategies' positions open on the same underlying:
    each re-seeds with its own strategy and exit rules (the underlying
    heuristic alone could not do this)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, _two_strategy_configs())

        store = PositionStore(db_path)
        pos_a = _broker_position(730.0, OptionRight.CALL, qty=1, avg_price=2.50)
        pos_b = _broker_position(725.0, OptionRight.PUT, qty=1, avg_price=0.40)
        pos_a.strategy_id = "strat_breakout"
        pos_b.strategy_id = "strat_straddle"
        pos_b.side = OrderSide.SELL  # short leg
        store.upsert_position(pos_a)
        store.upsert_position(pos_b)

        # "Restart": fresh runner, broker still holds both
        broker_b = _broker_position(725.0, OptionRight.PUT, qty=1, avg_price=0.40)
        broker_b.side = OrderSide.SELL
        broker = _mock_broker(positions=[
            _broker_position(730.0, OptionRight.CALL, qty=1, avg_price=2.50),
            broker_b,
        ])
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(), configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.3)

        seeded = {p.strategy_id: p for p in runner.position_manager.positions.values()}
        assert set(seeded) == {"strat_breakout", "strat_straddle"}

        # Each got ITS OWN strategy's stop rule (30% long vs 20% short)
        pa, pb = seeded["strat_breakout"], seeded["strat_straddle"]
        assert pa.stop_price == pytest.approx(2.50 * 0.70, abs=0.01)
        assert pb.stop_price == pytest.approx(0.40 * 1.20, abs=0.01)

        await runner.stop()
        runner_task.cancel()
