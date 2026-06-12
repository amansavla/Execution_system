import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.storage.event_log import EventStore
from src.core.enums import OrderSide, PositionStatus
from src.core.models import OptionContract, Position, QuoteSnapshot
from src.portfolio.exit_manager import ExitManager


def _make_contract(symbol: str = "SPX") -> OptionContract:
    return OptionContract(
        symbol=symbol, expiry="20260520", strike=5200.0, right="CALL", multiplier=100
    )


def _make_position(
    side: OrderSide = OrderSide.BUY,
    qty: int = 5,
    price: float = 3.00,
    **overrides,
) -> Position:
    # Entry time 60s ago so stop-loss grace period (10s) is not triggered
    entry_time = datetime.now(UTC) - timedelta(seconds=60)
    defaults = {
        "position_id": uuid4(),
        "strategy_id": "test_strat",
        "contract": _make_contract(),
        "side": side,
        "quantity": qty,
        "filled_quantity": qty,
        "average_entry_price": price,
        "status": PositionStatus.OPEN,
        "entry_order_id": uuid4(),
        "entry_time": entry_time,
        "created_at": entry_time,
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Position(**defaults)


class TestExitManager:
    def test_no_exit_triggered_when_conditions_are_fine(self):
        store = EventStore()
        em = ExitManager(store)

        pos = _make_position(side=OrderSide.BUY, price=3.00, stop_price=1.50, target_price=5.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=2.50, ask=2.70, timestamp=datetime.now(UTC))
        }

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 0

    def test_long_stop_loss_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        # Stop price = 2.00, bid drops to 1.90
        pos = _make_position(side=OrderSide.BUY, price=3.00, stop_price=2.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.90, ask=2.10, timestamp=datetime.now(UTC))
        }

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 1
        triggered_pos, intent, reason = exits[0]
        assert triggered_pos == pos
        assert reason == "stop_loss"
        assert intent.side == OrderSide.SELL
        assert intent.quantity == 5
        assert intent.is_entry is False
        assert intent.limit_price == 1.90  # bid (1.90) directly (no offset)

    def test_long_take_profit_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        # Target price = 4.00, bid rises to 4.10
        pos = _make_position(side=OrderSide.BUY, price=3.00, target_price=4.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=4.10, ask=4.20, timestamp=datetime.now(UTC))
        }

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 1
        _, intent, reason = exits[0]
        assert reason == "take_profit"
        assert intent.side == OrderSide.SELL
        assert intent.is_entry is False
        assert intent.limit_price == 4.10

    def test_short_stop_loss_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        # Short position (SELL side). Stop price = 4.00 (triggers if price rises above 4.00). Ask = 4.10
        pos = _make_position(side=OrderSide.SELL, price=3.00, stop_price=4.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=3.90, ask=4.10, timestamp=datetime.now(UTC))
        }

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 1
        _, intent, reason = exits[0]
        assert reason == "stop_loss"
        assert intent.side == OrderSide.BUY  # Buy to close
        assert intent.is_entry is False
        assert intent.limit_price == 4.10  # ask (4.10) directly (no offset)

    def test_short_take_profit_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        # Short position. Target price = 2.00 (triggers if price drops below 2.00). Ask = 1.90
        pos = _make_position(side=OrderSide.SELL, price=3.00, target_price=2.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.70, ask=1.90, timestamp=datetime.now(UTC))
        }

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 1
        _, intent, reason = exits[0]
        assert reason == "take_profit"
        assert intent.side == OrderSide.BUY
        assert intent.is_entry is False
        assert intent.limit_price == 1.90

    def test_time_exit_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        time_exit = datetime(2026, 5, 20, 19, 45, 0, tzinfo=UTC)
        pos = _make_position(time_exit_utc=time_exit)

        # 1. Current time is before exit time -> no exit
        exits_before = em.check_exits([pos], {}, current_time=datetime(2026, 5, 20, 19, 40, 0, tzinfo=UTC))
        assert len(exits_before) == 0

        # 2. Current time is after exit time but NO quote available ->
        #    exit is DEFERRED (no entry-price fallback; would produce
        #    limits outside NBBO). Re-triggers next tick once a quote exists.
        exits_no_quote = em.check_exits([pos], {}, current_time=datetime(2026, 5, 20, 19, 50, 0, tzinfo=UTC))
        assert len(exits_no_quote) == 0

        # 3. Current time after exit time WITH a quote -> exit triggered
        #    at the touch (sell -> bid).
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=3.10, ask=3.30, timestamp=datetime.now(UTC))
        }
        exits_after = em.check_exits([pos], quotes, current_time=datetime(2026, 5, 20, 19, 50, 0, tzinfo=UTC))
        assert len(exits_after) == 1
        _, intent, reason = exits_after[0]
        assert reason == "time_exit"
        assert intent.side == OrderSide.SELL
        assert intent.is_entry is False
        assert intent.limit_price == 3.10  # at the touch, no beyond-touch offset

    def test_strategy_driven_exit_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        pos = _make_position()
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=2.90, ask=3.10, timestamp=datetime.now(UTC))
        }
        exits = em.check_exits(
            [pos],
            quotes,
            current_time=datetime.now(UTC),
            strategy_exits={pos.position_id},
        )

        assert len(exits) == 1
        _, intent, reason = exits[0]
        assert reason == "strategy_exit"
        assert intent.is_entry is False
        assert intent.limit_price == 2.90  # at the touch

        # Without any quote, strategy exits are deferred (no fallback pricing)
        exits_no_quote = em.check_exits(
            [pos], {}, current_time=datetime.now(UTC), strategy_exits={pos.position_id},
        )
        assert len(exits_no_quote) == 0

    def test_force_flatten_all(self):
        store = EventStore()
        em = ExitManager(store)

        # Force-flatten is the one exit allowed to fall back to the
        # position's current (mark) price when no quote is available.
        pos1 = _make_position(symbol="SPX", current_price=3.05)
        pos2 = _make_position(symbol="QQQ", current_price=2.95)

        exits = em.check_exits(
            [pos1, pos2],
            {},
            current_time=datetime.now(UTC),
            force_flatten_all=True,
        )

        assert len(exits) == 2
        reasons = [item[2] for item in exits]
        assert all(r == "force_flatten" for r in reasons)
        assert exits[0][1].is_entry is False
        assert exits[1][1].is_entry is False
        assert exits[0][1].limit_price == 3.05
        assert exits[1][1].limit_price == 2.95


    def test_missing_required_quotes_does_not_trigger_exit(self):
        store = EventStore()
        em = ExitManager(store)

        # 1. Long position (needs bid). Bid is None. Ask is set. Stop/target would normally trigger if we fallback, but should not.
        pos_long = _make_position(side=OrderSide.BUY, price=3.00, stop_price=2.00, target_price=4.00)
        quotes_long = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=None, ask=1.90, timestamp=datetime.now(UTC))
        }
        exits_long = em.check_exits([pos_long], quotes_long, current_time=datetime.now(UTC))
        assert len(exits_long) == 0

        # 2. Short position (needs ask). Ask is None. Bid is set.
        pos_short = _make_position(side=OrderSide.SELL, price=3.00, stop_price=4.00, target_price=2.00)
        quotes_short = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=4.10, ask=None, timestamp=datetime.now(UTC))
        }
        exits_short = em.check_exits([pos_short], quotes_short, current_time=datetime.now(UTC))
        assert len(exits_short) == 0

    def test_use_mid_for_exits_trigger(self):
        store = EventStore()
        em = ExitManager(store)

        # Long position. Entry = 3.00, Stop = 2.00.
        # Bid drops to 1.90, Ask is 2.30. Mid = 2.10.
        # With use_mid_for_exits = True, it should not trigger stop loss because Mid (2.10) > Stop (2.00)
        pos_mid = _make_position(side=OrderSide.BUY, price=3.00, stop_price=2.00, use_mid_for_exits=True)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.90, ask=2.30, timestamp=datetime.now(UTC))
        }
        exits = em.check_exits([pos_mid], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 0

        # Now bid drops to 1.70, Ask to 2.10. Mid = 1.90.
        # Mid (1.90) <= Stop (2.00) -> stop loss TRIGGERS on the mid, but
        # the exit order is priced AT THE TOUCH (sell -> bid = 1.70) so it
        # is immediately marketable and stays inside NBBO.
        quotes_trigger = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.70, ask=2.10, timestamp=datetime.now(UTC))
        }
        exits_trigger = em.check_exits([pos_mid], quotes_trigger, current_time=datetime.now(UTC))
        assert len(exits_trigger) == 1
        _, intent, reason = exits_trigger[0]
        assert reason == "stop_loss"
        assert intent.limit_price == 1.70
    def test_exit_stale_quote_skipped(self):
        store = EventStore()
        em = ExitManager(store)

        pos = _make_position(side=OrderSide.BUY, price=3.00, stop_price=2.00)
        # Quote timestamp is 60 seconds old
        quote_time = datetime.now(UTC) - timedelta(seconds=60)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.90, ask=2.10, timestamp=quote_time)
        }

        # With max_age_seconds=10, the stale quote should be skipped and no exit triggered
        exits = em.check_exits(
            [pos], quotes, current_time=datetime.now(UTC), max_age_seconds=10.0
        )
        assert len(exits) == 0

    def test_exit_wide_spread_skipped(self):
        store = EventStore()
        em = ExitManager(store)

        # Stop price = 2.00. Quote bid is 1.80, ask is 3.60. Mid = 2.70. Spread = (3.60 - 1.80) / 2.70 = 66.6%.
        # Without spread limit, this would trigger stop loss (since bid 1.80 <= stop 2.00).
        pos = _make_position(side=OrderSide.BUY, price=3.00, stop_price=2.00)
        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=1.80, ask=3.60, timestamp=datetime.now(UTC))
        }

        # With max_spread_pct=20.0, the wide spread quote should be skipped and no exit triggered
        exits = em.check_exits(
            [pos], quotes, current_time=datetime.now(UTC), max_spread_pct=20.0
        )
        assert len(exits) == 0

    def test_hybrid_stop_loss_ignores_entry_bar_spikes(self):
        store = EventStore()
        em = ExitManager(store)

        from zoneinfo import ZoneInfo
        ny_tz = ZoneInfo("America/New_York")
        entry_time = datetime(2026, 6, 11, 10, 0, 15, tzinfo=ny_tz)
        current_time = datetime(2026, 6, 11, 10, 1, 5, tzinfo=ny_tz)

        pos = _make_position(
            side=OrderSide.SELL,
            price=3.00,
            stop_price=4.00,
            entry_time=entry_time.astimezone(UTC),
            created_at=entry_time.astimezone(UTC)
        )

        quotes = {
            "SPX": QuoteSnapshot(symbol="SPX", bid=3.00, ask=3.16, timestamp=current_time.astimezone(UTC))
        }

        # Completed bar for 10:00:00 (the entry minute) with a high of 4.18 (above stop 4.00)
        class MockBar:
            def __init__(self):
                self.symbol = "SPX"
                self.minute_start_ny = datetime(2026, 6, 11, 10, 0, 0, tzinfo=ny_tz)
                self.open = 3.00
                self.high = 4.18
                self.low = 3.00
                self.close = 3.08
                self.volume = 100

        bars = {
            "SPX": MockBar()
        }

        exits = em.check_exits(
            [pos],
            quotes,
            current_time=current_time.astimezone(UTC),
            bars=bars
        )
        # Should NOT trigger stop-loss because the entry-minute completed bar is ignored!
        assert len(exits) == 0




def test_exit_manager_boundary_isolation():
    """Verify ExitManager does not import BrokerClient or OrderManager."""
    import src.portfolio.exit_manager as em
    source = inspect.getsource(em)

    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "broker" not in stripped.lower(), (
                "Forbidden BrokerClient import in exit_manager.py"
            )
            assert "order_manager" not in stripped.lower(), (
                "Forbidden OrderManager import in exit_manager.py"
            )
