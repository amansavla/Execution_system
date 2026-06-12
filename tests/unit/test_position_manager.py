"""Unit tests for PositionManager.

Covers:
- Position creation from FillEvent
- Multiple simultaneous positions
- Quantity, average entry price, realized/unrealized PnL tracking
- Partial exits, full exits, and forced exits
- Setting stop/target/time exit rules
- Boundary isolation (no BrokerClient or OrderManager imports)
"""

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.storage.event_log import EventStore
from src.core.enums import OrderSide, PositionStatus
from src.core.models import OptionContract, FillEvent, Position
from src.portfolio.position_manager import PositionManager


def _make_contract(symbol: str = "SPX") -> OptionContract:
    return OptionContract(
        symbol=symbol, expiry="20260520", strike=5200.0, right="CALL", multiplier=100
    )


def _make_fill(
    strategy_id: str = "test_strat",
    symbol: str = "SPX",
    side: OrderSide = OrderSide.BUY,
    qty: int = 5,
    price: float = 3.50,
) -> FillEvent:
    return FillEvent(
        order_id=uuid4(),
        strategy_id=strategy_id,
        contract=_make_contract(symbol),
        side=side,
        filled_quantity=qty,
        fill_price=price,
        timestamp=datetime.now(UTC),
    )


class TestPositionManager:
    def test_create_position_from_fill_event(self):
        store = EventStore()
        pm = PositionManager(store)

        fill = _make_fill(side=OrderSide.BUY, qty=5, price=3.50)
        pos = pm.handle_fill(fill)

        assert pos.position_id is not None
        assert pos.strategy_id == "test_strat"
        assert pos.contract.symbol == "SPX"
        assert pos.side == OrderSide.BUY
        assert pos.quantity == 5
        assert pos.average_entry_price == 3.50
        assert pos.status == PositionStatus.OPEN
        assert pos.entry_order_id == fill.order_id
        assert pos.entry_time == fill.timestamp

        # Check in dict
        assert pm.get_position(pos.position_id) == pos
        assert len(pm.get_open_positions()) == 1

    def test_supports_multiple_simultaneous_positions(self):
        store = EventStore()
        pm = PositionManager(store)

        # Position 1: SPX
        fill1 = _make_fill(strategy_id="strat_1", symbol="SPX")
        pos1 = pm.handle_fill(fill1)

        # Position 2: QQQ
        fill2 = _make_fill(strategy_id="strat_2", symbol="QQQ")
        pos2 = pm.handle_fill(fill2)

        assert len(pm.get_open_positions()) == 2
        assert pm.get_position(pos1.position_id) == pos1
        assert pm.get_position(pos2.position_id) == pos2

    def test_adding_to_position_updates_avg_price(self):
        store = EventStore()
        pm = PositionManager(store)

        # First entry: 5 contracts at 3.00
        fill1 = _make_fill(qty=5, price=3.00)
        pos = pm.handle_fill(fill1)

        # Second entry (same position): 5 contracts at 4.00
        # Pass position_id to ensure it target same position
        fill2 = _make_fill(qty=5, price=4.00)
        updated_pos = pm.handle_fill(fill2, position_id=pos.position_id)

        assert updated_pos.quantity == 10
        # Weighted average entry price: ((5 * 3) + (5 * 4)) / 10 = 3.50
        assert updated_pos.average_entry_price == 3.50
        assert updated_pos.status == PositionStatus.OPEN

    def test_partial_exit_realized_pnl(self):
        store = EventStore()
        pm = PositionManager(store)

        # Entry: BUY 5 contracts at 3.00
        fill_entry = _make_fill(side=OrderSide.BUY, qty=5, price=3.00)
        pos = pm.handle_fill(fill_entry)

        # Partial Exit: SELL 2 contracts at 4.50
        fill_exit = _make_fill(side=OrderSide.SELL, qty=2, price=4.50)
        updated_pos = pm.handle_fill(fill_exit, position_id=pos.position_id)

        assert updated_pos.quantity == 3  # 5 - 2
        # Realized PnL: (4.50 - 3.00) * 2 contracts * 100 multiplier = 300.00
        assert updated_pos.realized_pnl == 300.00
        assert updated_pos.status == PositionStatus.OPEN
        assert fill_exit.order_id in updated_pos.exit_order_ids

    def test_full_exit_closes_position(self):
        store = EventStore()
        pm = PositionManager(store)

        # Entry: BUY 5 contracts at 3.00
        fill_entry = _make_fill(side=OrderSide.BUY, qty=5, price=3.00)
        pos = pm.handle_fill(fill_entry)

        # Full Exit: SELL 5 contracts at 2.50 (Loss)
        fill_exit = _make_fill(side=OrderSide.SELL, qty=5, price=2.50)
        updated_pos = pm.handle_fill(fill_exit, position_id=pos.position_id)

        assert updated_pos.quantity == 0
        # Realized PnL: (2.50 - 3.00) * 5 contracts * 100 multiplier = -250.00
        assert updated_pos.realized_pnl == -250.00
        assert updated_pos.status == PositionStatus.CLOSED
        assert updated_pos.exit_time == fill_exit.timestamp
        assert len(pm.get_open_positions()) == 0

    def test_forced_exit(self):
        store = EventStore()
        pm = PositionManager(store)

        # Entry: BUY 5 contracts at 3.00
        fill_entry = _make_fill(side=OrderSide.BUY, qty=5, price=3.00)
        pos = pm.handle_fill(fill_entry)

        # Force close position at 4.00
        exit_time = datetime.now(UTC)
        closed_pos = pm.force_close_position(pos.position_id, exit_price=4.00, timestamp=exit_time)

        assert closed_pos.quantity == 0
        # Realized PnL: (4.00 - 3.00) * 5 * 100 = 500.00
        assert closed_pos.realized_pnl == 500.00
        assert closed_pos.status == PositionStatus.CLOSED
        assert closed_pos.exit_time == exit_time
        assert len(pm.get_open_positions()) == 0

    def test_setting_exit_rules(self):
        store = EventStore()
        pm = PositionManager(store)

        fill = _make_fill()
        pos = pm.handle_fill(fill)

        time_exit = datetime.now(UTC)
        pm.set_exit_rules(
            pos.position_id,
            stop_price=1.50,
            target_price=6.00,
            time_exit_utc=time_exit,
        )

        assert pos.stop_price == 1.50
        assert pos.target_price == 6.00
        assert pos.time_exit_utc == time_exit

    def test_unrealized_pnl_updates(self):
        from src.core.models import QuoteSnapshot

        store = EventStore()
        pm = PositionManager(store)

        fill = _make_fill(side=OrderSide.BUY, qty=5, price=3.00)
        pos = pm.handle_fill(fill)

        # Update market price with mid quote price of 3.50 (bid=3.40, ask=3.60)
        quotes = {
            "SPX": QuoteSnapshot(
                symbol="SPX", bid=3.40, ask=3.60, timestamp=datetime.now(UTC)
            )
        }
        pm.update_position_prices(quotes)

        # Mid price = (3.40 + 3.60) / 2 = 3.50
        # Unrealized PnL = (3.50 - 3.00) * 5 * 100 = 250.00
        assert pos.current_price == 3.50
        assert pos.unrealized_pnl == 250.00


def test_position_manager_boundary_isolation():
    """Verify PositionManager does not import BrokerClient or OrderManager."""
    import src.portfolio.position_manager as pm
    source = inspect.getsource(pm)

    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "broker" not in stripped.lower(), (
                "Forbidden BrokerClient import in position_manager.py"
            )
            assert "order_manager" not in stripped.lower(), (
                "Forbidden OrderManager import in position_manager.py"
            )
