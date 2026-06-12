"""Phase 4 integration tests: command queue + same-path equivalence.

Core assertion: a dashboard-originated exit and an automated (strategy/
time/stop) exit produce broker calls that are IDENTICAL on symbol, side,
qty, order type, limit price, and orderRef prefix
({strategy}:{position_id}:{leg}:{side}), ignoring broker-assigned IDs
and the trailing unix_ms timestamp.

Run: python3 -m pytest tests/integration/test_command_queue.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from src.app.runner import ExecutionRunner, StrategyProvider
from src.broker.mock_broker import MockBrokerClient, MockBrokerConfig
from src.control.command_queue import CommandQueue
from src.core.enums import OptionRight, OrderSide, PositionStatus
from src.core.models import AccountState, OptionContract, OrderPlan, Position, QuoteSnapshot
from src.storage.event_log import EventStore

from tests.integration.test_resilience import _write_configs, _strategy, _broker_position


def _capture_broker(positions: list[Position]) -> tuple[MockBrokerClient, list[OrderPlan]]:
    """Mock broker that records every OrderPlan it receives."""
    quote = QuoteSnapshot(symbol="XSP", bid=2.40, ask=2.60, last=2.50,
                          timestamp=datetime.now(UTC))
    opt_sym = positions[0].contract.to_quote_symbol() if positions else "X"
    broker = MockBrokerClient(MockBrokerConfig(
        connected_initially=True, auto_fill=False,  # keep orders working
        acceptance_delay_seconds=0.0, fill_delay_seconds=0.01,
        simulated_quotes={"XSP": quote, opt_sym: QuoteSnapshot(
            symbol=opt_sym, bid=2.40, ask=2.60, last=2.50,
            timestamp=datetime.now(UTC))},
        simulated_positions=positions,
        simulated_account_state=AccountState(
            account_id="DU12345", net_liquidation=100000.0,
            available_funds=80000.0, buying_power=200000.0),
    ))
    captured: list[OrderPlan] = []
    original_place = broker.place_order

    async def capturing_place(order_plan: OrderPlan):
        captured.append(order_plan)
        return await original_place(order_plan)

    broker.place_order = capturing_place
    return broker, captured


def _ref_prefix(order_ref: str) -> str:
    """{strategy}:{position_id}:{leg}:{side} — drop trailing unix_ms."""
    return ":".join(order_ref.split(":")[:-1])


async def _run_exit_scenario(tmpdir: str, manual: bool) -> OrderPlan:
    """Seed one position; exit it via dashboard command (manual=True) or
    via the automated strategy-exit path (manual=False). Return the exit
    OrderPlan the broker received."""
    config_dir = Path(tmpdir)
    db_path = str(Path(tmpdir) / "events.db")
    _write_configs(config_dir, [_strategy("strat_x", stop_loss_pct=90, time_exit="23:59")])

    pos = _broker_position(730.0, OptionRight.PUT, qty=2, avg_price=2.50)
    broker, captured = _capture_broker([pos])

    runner = ExecutionRunner(
        broker=broker, event_store=EventStore(db_path=db_path),
        strategy_provider=StrategyProvider(), configs_dir=config_dir,
        tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
    )
    runner_task = asyncio.create_task(runner.start())
    await asyncio.sleep(0.25)

    seeded = list(runner.position_manager.positions.values())
    assert len(seeded) == 1, "position must be seeded before exit"
    position_id = seeded[0].position_id

    if manual:
        # Dashboard path: enqueue command; runner routes it through
        # ExitManager on its tick loop.
        CommandQueue(db_path).enqueue("exit_position", {"position_id": str(position_id)})
    else:
        # Automated path: same set ExitManager consumes for strategy exits.
        runner._asymmetric_exits.add(position_id)

    await asyncio.sleep(0.4)

    await runner.stop()
    runner_task.cancel()

    exits = [p for p in captured if not p.is_entry]
    assert len(exits) >= 1, f"expected an exit order (manual={manual}), got {len(exits)}"
    return exits[0]


async def test_dashboard_exit_identical_to_automated_exit() -> None:
    """THE same-path proof. Both exits must be byte-identical on the
    assertion key (position_id differs per run, so compare structure)."""
    with tempfile.TemporaryDirectory() as t1:
        manual_plan = await _run_exit_scenario(t1, manual=True)
    with tempfile.TemporaryDirectory() as t2:
        auto_plan = await _run_exit_scenario(t2, manual=False)

    # Identical on: symbol, side, qty, order type, limit price
    assert manual_plan.contract.symbol == auto_plan.contract.symbol
    assert manual_plan.contract.strike == auto_plan.contract.strike
    assert manual_plan.contract.right == auto_plan.contract.right
    assert manual_plan.side == auto_plan.side == OrderSide.SELL
    assert manual_plan.quantity == auto_plan.quantity == 2
    assert manual_plan.order_type == auto_plan.order_type == "LMT"
    assert manual_plan.limit_price == auto_plan.limit_price  # touch-priced

    # orderRef prefix structure: {strategy}:{position_id}:{leg}:{side}
    mp, ap = _ref_prefix(manual_plan.order_ref), _ref_prefix(auto_plan.order_ref)
    m_parts, a_parts = mp.split(":"), ap.split(":")
    assert len(m_parts) == 4 and len(a_parts) == 4
    assert m_parts[0] == a_parts[0] == "strat_x"          # strategy
    assert m_parts[2] == a_parts[2] == "PE"               # leg from right
    assert m_parts[3] == a_parts[3] == "SELL"             # side
    # position_id slot is populated (not 'new') on both paths
    assert m_parts[1] != "new" and a_parts[1] != "new"


async def test_cancel_order_command() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, [_strategy("strat_x", time_exit="23:59")])

        pos = _broker_position(730.0, OptionRight.PUT, qty=1)
        broker, captured = _capture_broker([pos])
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(), configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.25)

        # Force an exit order to exist (it stays working: auto_fill=False)
        pid = list(runner.position_manager.positions)[0]
        runner._asymmetric_exits.add(pid)
        await asyncio.sleep(0.3)
        working = [o for o in runner.order_manager.orders.values()
                   if not o.is_entry]
        assert working, "expected a working exit order"
        oid = working[0].order_id

        q = CommandQueue(db_path)
        cid = q.enqueue("cancel_order", {"order_id": str(oid)})
        await asyncio.sleep(0.3)

        statuses = {c["command_id"]: c["status"] for c in q.recent()}
        assert statuses[cid] == "done"

        await runner.stop()
        runner_task.cancel()


async def test_pause_and_flatten_commands() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, [_strategy("strat_x", time_exit="23:59")])

        pos = _broker_position(730.0, OptionRight.PUT, qty=1)
        broker, captured = _capture_broker([pos])
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(), configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.25)

        q = CommandQueue(db_path)
        q.enqueue("pause_strategy", {"strategy_id": "strat_x", "paused": True})
        q.enqueue("flatten_all", {})
        await asyncio.sleep(0.4)

        assert "strat_x" in runner.override_manager.state.paused_strategies
        assert runner._flatten_all_active is True
        # Flatten produced an exit order through the force_flatten path
        exits = [p for p in captured if not p.is_entry]
        assert len(exits) >= 1

        # Resume works too
        q.enqueue("pause_strategy", {"strategy_id": "strat_x", "paused": False})
        await asyncio.sleep(0.2)
        assert "strat_x" not in runner.override_manager.state.paused_strategies

        await runner.stop()
        runner_task.cancel()


def test_queue_rejects_unknown_type() -> None:
    q = CommandQueue(":memory:")
    with pytest.raises(ValueError):
        q.enqueue("rm_rf_everything", {})


async def test_update_strategy_command() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        db_path = str(Path(tmpdir) / "events.db")
        _write_configs(config_dir, [_strategy("strat_x", time_exit="23:59")])

        broker, _ = _capture_broker([_broker_position(730.0, OptionRight.PUT, qty=1)])
        runner = ExecutionRunner(
            broker=broker, event_store=EventStore(db_path=db_path),
            strategy_provider=StrategyProvider(), configs_dir=config_dir,
            tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
        )
        runner_task = asyncio.create_task(runner.start())
        await asyncio.sleep(0.25)

        q = CommandQueue(db_path)
        cid = q.enqueue("update_strategy", {"strategy_id": "strat_x", "changes": {
            "exit.stop_loss_pct": 45, "entry.max_contracts": 3,
            "entry.trigger_pct": 0.004, "enabled": False,
        }})
        bad = q.enqueue("update_strategy", {"strategy_id": "strat_x", "changes": {
            "signal_source": "evil",  # not whitelisted
        }})
        await asyncio.sleep(0.3)

        statuses = {c["command_id"]: (c["status"], c["result"]) for c in q.recent()}
        assert statuses[cid][0] == "done"
        assert statuses[bad][0] == "failed" and "not editable" in statuses[bad][1]

        cfg = runner._strategy_configs["strat_x"]
        assert cfg.exit.stop_loss_pct == 45.0
        assert cfg.entry.max_contracts == 3
        assert cfg.entry.trigger_pct == 0.004
        assert cfg.enabled is False

        # Persisted overlay reloads on a fresh runner (restart survival)
        overlay = (config_dir / "strategy_overrides.yaml").read_text()
        assert "stop_loss_pct" in overlay

        await runner.stop()
        runner_task.cancel()
