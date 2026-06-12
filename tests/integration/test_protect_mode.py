"""Protect mode: a system lock must never abandon open positions.

While locked: NO new entries, but exit management, dashboard commands
(incl. unlock), reconciliation and state publishing keep running.

Run: python3 -m pytest tests/integration/test_protect_mode.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from src.app.runner import ExecutionRunner, StrategyProvider
from src.control.command_queue import CommandQueue
from src.core.enums import OptionRight, OrderSide, SignalDirection
from src.core.models import OptionContract, QuoteSnapshot, StrategySignal
from src.storage.event_log import EventStore

from tests.integration.test_resilience import (
    _broker_position,
    _mock_broker,
    _strategy,
    _write_configs,
)


class AlwaysSignalProvider(StrategyProvider):
    """Emits a signal on every poll (to prove entries are blocked)."""

    def __init__(self) -> None:
        self.polls = 0

    async def poll(self, strategy_config, current_time):
        self.polls += 1
        return [StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.LONG,
            contract=OptionContract(symbol="XSP", expiry="20990101",
                                    strike=730.0, right=OptionRight.CALL,
                                    multiplier=100),
            requested_quantity=1, limit_price=2.50,
            timestamp=current_time,
        )]


async def test_locked_system_still_manages_exits_and_commands() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, [_strategy("strat_x", time_exit="23:59")])

        # Open broker position seeded at startup
        pos = _broker_position(730.0, OptionRight.PUT, qty=1, avg_price=2.50)
        broker = _mock_broker(positions=[pos])
        opt_sym = pos.contract.to_quote_symbol()
        broker.config.simulated_quotes[opt_sym] = QuoteSnapshot(
            symbol=opt_sym, bid=2.40, ask=2.60, last=2.50,
            timestamp=datetime.now(UTC))

        provider = AlwaysSignalProvider()
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=provider, configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.25)
        assert len(runner.position_manager.positions) == 1
        pid = list(runner.position_manager.positions)[0]

        # LOCK the system manually (worst-case provenance: not auto-cleared)
        runner.override_manager.lock_system()
        await asyncio.sleep(0.15)
        polls_at_lock = provider.polls

        # 1. Ticks continue while locked
        t0 = runner._tick_count
        await asyncio.sleep(0.2)
        assert runner._tick_count > t0, "tick loop must keep running while locked"

        # 2. No NEW entries while locked (strategy not even polled)
        assert provider.polls == polls_at_lock

        # 3. Dashboard commands still processed: exit the open position
        q = CommandQueue(db_path)
        cid = q.enqueue("exit_position", {"position_id": str(pid)})
        await asyncio.sleep(0.4)
        statuses = {c["command_id"]: c["status"] for c in q.recent()}
        assert statuses[cid] == "done", "commands must work while locked"
        # The exit order went out through ExitManager despite the lock
        exits = [o for o in runner.order_manager.orders.values() if not o.is_entry]
        assert len(exits) >= 1, "exit order must be submitted while locked"

        # 4. Operator unlock via dashboard command (reconciliation-gated)
        cid2 = q.enqueue("unlock_system", {})
        await asyncio.sleep(0.4)
        statuses = {c["command_id"]: c["status"] for c in q.recent()}
        # MockBroker book == internal book -> reconciliation clean -> unlocked
        assert statuses[cid2] == "done"
        assert runner.override_manager.state.system_locked is False

        await runner.stop()
        runner_task.cancel()
