"""Tests for 1-min BarBuilder and hybrid stop-loss evaluation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.core.enums import OrderSide, PositionStatus
from src.core.models import OptionContract, Position, QuoteSnapshot
from src.marketdata.bars import BarBuilder
from src.portfolio.exit_manager import ExitManager
from src.storage.event_log import EventStore

_NY = ZoneInfo("America/New_York")


def _ts(h, m, s):
    return datetime(2026, 6, 10, h, m, s, tzinfo=_NY).astimezone(UTC)


class TestBarBuilder:
    def test_bars_aggregate_by_ny_minute(self):
        bb = BarBuilder()
        bb.on_tick("X", 10.0, _ts(10, 0, 5))
        bb.on_tick("X", 10.5, _ts(10, 0, 20))
        bb.on_tick("X", 9.8, _ts(10, 0, 45))
        bb.on_tick("X", 10.2, _ts(10, 0, 59))
        # next minute tick rolls the previous bar
        bb.on_tick("X", 10.3, _ts(10, 1, 1))

        bar = bb.get_latest_completed_bar("X", now=_ts(10, 1, 2))
        assert bar is not None
        assert bar.open == 10.0
        assert bar.high == 10.5
        assert bar.low == 9.8
        assert bar.close == 10.2
        assert bar.tick_count == 4

    def test_stale_working_bar_completes_on_read(self):
        bb = BarBuilder()
        bb.on_tick("X", 5.0, _ts(10, 0, 30))
        # No further ticks; reading two minutes later must surface the bar
        bar = bb.get_latest_completed_bar("X", now=_ts(10, 2, 0))
        assert bar is not None
        assert bar.close == 5.0

    def test_incomplete_minute_not_exposed(self):
        bb = BarBuilder()
        bb.on_tick("X", 5.0, _ts(10, 0, 30))
        assert bb.get_latest_completed_bar("X", now=_ts(10, 0, 45)) is None

    def test_out_of_order_tick_ignored(self):
        bb = BarBuilder()
        bb.on_tick("X", 5.0, _ts(10, 1, 0))
        bb.on_tick("X", 99.0, _ts(10, 0, 59))  # late tick from prior minute
        bb.on_tick("X", 5.2, _ts(10, 2, 0))
        bar = bb.get_latest_completed_bar("X", now=_ts(10, 2, 1))
        assert bar.high == 5.0  # 99.0 never polluted the working bar


def _pos(side=OrderSide.SELL, entry=1.00, stop=1.20):
    entry_time = datetime.now(UTC) - timedelta(minutes=5)
    return Position(
        position_id=uuid4(),
        strategy_id="s",
        contract=OptionContract(symbol="XSP", expiry="20260610", strike=729.0, right="CALL", multiplier=100),
        side=side,
        quantity=1,
        filled_quantity=1,
        average_entry_price=entry,
        stop_price=stop,
        status=PositionStatus.OPEN,
        entry_order_id=uuid4(),
        entry_time=entry_time,
        created_at=entry_time,
        updated_at=datetime.now(UTC),
    )


class TestHybridStopLoss:
    def _quote(self, bid, ask):
        sym = "XSP_20260610_729.0_CALL"
        contract = OptionContract(symbol="XSP", expiry="20260610", strike=729.0, right="CALL", multiplier=100)
        key = contract.to_quote_symbol()
        return {key: QuoteSnapshot(symbol=key, bid=bid, ask=ask, timestamp=datetime.now(UTC))}, key

    def _bar(self, key, low, high, close, minutes_ago=1):
        bb = BarBuilder()
        t = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        bb.on_tick(key, close, t)
        bar = bb._working[key]
        bar.low, bar.high = low, high
        return bar

    def test_bar_high_triggers_short_stop(self):
        em = ExitManager(EventStore())
        pos = _pos(side=OrderSide.SELL, entry=1.00, stop=1.20)
        quotes, key = self._quote(bid=1.05, ask=1.15)  # ask < stop: tick eval would NOT fire
        bar = self._bar(key, low=0.95, high=1.25, close=1.10)  # bar high breached stop

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC), bars={key: bar})
        assert len(exits) == 1
        _, intent, reason = exits[0]
        assert reason == "stop_loss"
        assert intent.limit_price == 1.15  # buy-back at the ask (touch)

    def test_quiet_bar_no_trigger_even_if_ask_spikes(self):
        """Wide ask alone (spread noise) must NOT stop out when the bar of
        traded prices never breached the stop — the core backtest-fidelity
        property."""
        em = ExitManager(EventStore())
        pos = _pos(side=OrderSide.SELL, entry=1.00, stop=1.20)
        quotes, key = self._quote(bid=0.90, ask=1.30)  # ask 1.30 > stop, mid=1.10
        bar = self._bar(key, low=0.95, high=1.10, close=1.05)  # trades stayed calm

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC), bars={key: bar})
        assert len(exits) == 0

    def test_intrabar_guard_fires_on_extreme_mid(self):
        em = ExitManager(EventStore())
        pos = _pos(side=OrderSide.SELL, entry=1.00, stop=1.20)
        # mid = 1.45 >= 1.20 * 1.15 = 1.38 -> guard fires despite calm bar
        quotes, key = self._quote(bid=1.40, ask=1.50)
        bar = self._bar(key, low=0.95, high=1.10, close=1.05)

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC), bars={key: bar})
        assert len(exits) == 1
        assert exits[0][2] == "stop_loss"

    def test_legacy_tick_eval_without_bars(self):
        em = ExitManager(EventStore())
        pos = _pos(side=OrderSide.SELL, entry=1.00, stop=1.20)
        quotes, key = self._quote(bid=1.15, ask=1.25)  # ask >= stop

        exits = em.check_exits([pos], quotes, current_time=datetime.now(UTC))
        assert len(exits) == 1
        assert exits[0][2] == "stop_loss"
