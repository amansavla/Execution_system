"""Multi-leg coordination over replacement chains.

Regression tests for the 2026-06-11 live incidents: routine repricer
cancel/replace cycles were read as failed entry legs, nuking healthy
straddle peer legs and flattening live positions.

Run: python3 -m pytest tests/integration/test_multileg_coordination.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.app.runner import ExecutionRunner, StrategyProvider
from src.core.enums import OptionRight, OrderSide, OrderStatus, PositionStatus
from src.core.models import OptionContract, OrderState, Position
from src.storage.event_log import EventStore

from tests.integration.test_resilience import _mock_broker, _strategy, _write_configs


def _entry_order(strategy_id: str, strike: float, right: OptionRight,
                 status: OrderStatus, filled: int = 0, qty: int = 2) -> OrderState:
    return OrderState(
        order_plan_id=uuid4(),
        position_id=None,
        is_entry=True,
        strategy_id=strategy_id,
        contract=OptionContract(symbol="XSP", expiry="20990101", strike=strike,
                                right=right, multiplier=100),
        side=OrderSide.SELL,
        quantity=qty,
        filled_quantity=filled,
        limit_price=3.00,
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _open_position(strategy_id: str, strike: float, right: OptionRight,
                   entry_order_id) -> Position:
    return Position(
        position_id=uuid4(), strategy_id=strategy_id,
        contract=OptionContract(symbol="XSP", expiry="20990101", strike=strike,
                                right=right, multiplier=100),
        side=OrderSide.SELL, quantity=2, filled_quantity=2,
        average_entry_price=3.0, status=PositionStatus.OPEN,
        entry_order_id=entry_order_id,
        entry_time=datetime.now(UTC), created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def _make_runner(tmpdir: str) -> ExecutionRunner:
    config_dir = Path(tmpdir)
    _write_configs(config_dir, [_strategy("straddle_x", time_exit="23:59")])
    runner = ExecutionRunner(
        broker=_mock_broker(), event_store=EventStore(),
        strategy_provider=StrategyProvider(), configs_dir=config_dir,
        tick_interval_seconds=0.01, reconciliation_interval_seconds=999,
    )
    task = asyncio.create_task(runner.start())
    await asyncio.sleep(0.15)
    runner._test_task = task
    return runner


async def test_repricer_replacement_is_not_a_failed_leg() -> None:
    """Cancel/replace of one leg must NOT cancel the peer leg or flatten."""
    with tempfile.TemporaryDirectory() as tmpdir:
        runner = await _make_runner(tmpdir)
        om = runner.order_manager

        # Leg A: original order CANCELLED but SUPERSEDED by a working
        # replacement (the repricer's routine cancel/replace).
        leg_a_old = _entry_order("straddle_x", 730.0, OptionRight.CALL,
                                 OrderStatus.CANCELLED, filled=0)
        leg_a_new = _entry_order("straddle_x", 730.0, OptionRight.CALL,
                                 OrderStatus.SUBMITTED, filled=0)
        leg_a_old.superseded_by = leg_a_new.order_id
        # Leg B: untouched working order
        leg_b = _entry_order("straddle_x", 728.0, OptionRight.PUT,
                             OrderStatus.SUBMITTED, filled=0)
        for o in (leg_a_old, leg_a_new, leg_b):
            om.orders[o.order_id] = o

        await runner._manage_active_orders(datetime.now(UTC))

        # Peer leg must still be working, replacement untouched, no flatten
        assert leg_b.status == OrderStatus.SUBMITTED
        assert leg_a_new.status == OrderStatus.SUBMITTED
        assert runner._asymmetric_exits == set()

        await runner.stop()
        runner._test_task.cancel()


async def test_true_leg_failure_cancels_peers_and_flattens() -> None:
    """A chain-terminal zero-fill cancel IS a failure: cancel peer, flatten."""
    with tempfile.TemporaryDirectory() as tmpdir:
        runner = await _make_runner(tmpdir)
        om = runner.order_manager

        # Leg A failed terminally (no successor, zero filled)
        leg_a = _entry_order("straddle_x", 730.0, OptionRight.CALL,
                             OrderStatus.CANCELLED, filled=0)
        # Leg B still working
        leg_b = _entry_order("straddle_x", 728.0, OptionRight.PUT,
                             OrderStatus.SUBMITTED, filled=0)
        # Leg C already filled into a position
        leg_c = _entry_order("straddle_x", 729.0, OptionRight.PUT,
                             OrderStatus.FILLED, filled=2)
        for o in (leg_a, leg_b, leg_c):
            om.orders[o.order_id] = o
        pos = _open_position("straddle_x", 729.0, OptionRight.PUT, leg_c.order_id)
        runner.position_manager.positions[pos.position_id] = pos

        await runner._manage_active_orders(datetime.now(UTC))

        # Working peer was hard-cancelled; filled peer flagged asymmetric
        assert leg_b.status in (OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED)
        assert leg_b.metadata.get("hard_cancel") is True
        assert pos.position_id in runner._asymmetric_exits

        await runner.stop()
        runner._test_task.cancel()


async def test_partial_fill_own_chain_not_flattened() -> None:
    """Partial entry fill + cancelled remainder is a VALID position — the
    position's own chain must never flag it asymmetric."""
    with tempfile.TemporaryDirectory() as tmpdir:
        runner = await _make_runner(tmpdir)
        om = runner.order_manager

        # Single-leg entry: partially filled then remainder cancelled
        entry = _entry_order("straddle_x", 730.0, OptionRight.CALL,
                             OrderStatus.CANCELLED, filled=1, qty=2)
        om.orders[entry.order_id] = entry
        pos = _open_position("straddle_x", 730.0, OptionRight.CALL, entry.order_id)
        runner.position_manager.positions[pos.position_id] = pos

        await runner._manage_active_orders(datetime.now(UTC))

        # filled=1 -> not a zero-fill failure; nothing flagged
        assert runner._asymmetric_exits == set()

        await runner.stop()
        runner._test_task.cancel()
