"""Unit tests for the 5 EMA breakout strategy provider."""

from datetime import UTC, datetime, time, timedelta
from typing import Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from src.core.config import (
    StrategyConfig,
    StrategyEntryConfig,
    StrategyExitConfig,
)
from src.core.enums import OptionRight, OrderSide, PositionStatus
from src.core.models import OptionContract, Position, QuoteSnapshot
from src.marketdata.bars import Bar
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore
from src.strategies.xsp_5_ema import (
    FALLBACK_PREMIUM_STOP_PCT,
    XSP5EMAStrategyProvider,
)

_NY = ZoneInfo("America/New_York")
# 2026-06-12 is a Friday
DAY = datetime(2026, 6, 12).date()


def ny(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss, tzinfo=_NY)


def one_min_bars(start: datetime, ohlc_per_min: list[tuple]) -> list[Bar]:
    """Build consecutive 1-min Bars starting at `start` (NY)."""
    bars = []
    for i, (o, h, l, c) in enumerate(ohlc_per_min):
        bars.append(Bar(
            symbol="XSP", minute_start_ny=start + timedelta(minutes=i),
            open=o, high=h, low=l, close=c, tick_count=10,
        ))
    return bars


def flat_5min(start: datetime, price: float) -> list[Bar]:
    """Five identical 1-min bars (a flat 5-min candle at `price`)."""
    return one_min_bars(start, [(price, price, price, price)] * 5)


class FakeBroker:
    """Minimal broker stub: bars, quotes, historical closes, account."""

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.underlying_quote: Optional[QuoteSnapshot] = None
        self.option_quotes: dict[str, QuoteSnapshot] = {}
        self.historical: dict[str, float] = {}  # "HH:MM" -> close
        self.historical_calls: list[datetime] = []
        self.net_liq = 100_000.0

    def get_completed_bars(self, symbol: str, count: int = 60) -> list[Bar]:
        return self.bars[-count:]

    def get_latest_completed_bar(self, symbol: str) -> Optional[Bar]:
        return self.bars[-1] if self.bars else None

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        out = {}
        for s in symbols:
            if s == "XSP" and self.underlying_quote is not None:
                out[s] = self.underlying_quote
            elif s in self.option_quotes:
                out[s] = self.option_quotes[s]
            else:
                # default option quote for sizing
                out[s] = QuoteSnapshot(
                    symbol=s, bid=2.4, ask=2.6, last=2.5,
                    timestamp=datetime.now(UTC),
                )
        return out

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        self.historical_calls.append(end_time)
        return self.historical.get(end_time.astimezone(_NY).strftime("%H:%M"))

    async def get_account_state(self):
        class _Acct:
            net_liquidation = self.net_liq
        return _Acct()


def make_config(strategy_id: str = "xsp_5ema_base") -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        enabled=True,
        underlying="XSP",
        allow_reentry=True,
        entry=StrategyEntryConfig(
            signal_source="xsp_5_ema", max_contracts=20,
            order_timeout_seconds=90,
        ),
        exit=StrategyExitConfig(time_exit_utc="15:45"),
        position_sizing_pct=0.01,
    )


def make_position(
    strategy_id: str = "xsp_5ema_base",
    right: OptionRight = OptionRight.CALL,
    entry_price: float = 2.50,
) -> Position:
    entry_time = datetime.now(UTC) - timedelta(seconds=60)
    return Position(
        position_id=uuid4(),
        strategy_id=strategy_id,
        contract=OptionContract(
            symbol="XSP", expiry=DAY.strftime("%Y%m%d"), strike=600.0,
            right=right, multiplier=100,
        ),
        side=OrderSide.BUY,
        quantity=2,
        filled_quantity=2,
        average_entry_price=entry_price,
        status=PositionStatus.OPEN,
        entry_order_id=uuid4(),
        entry_time=entry_time,
        created_at=entry_time,
    )


def make_provider(broker=None, with_pm=True):
    broker = broker or FakeBroker()
    provider = XSP5EMAStrategyProvider(broker=broker)
    pm = None
    if with_pm:
        pm = PositionManager(EventStore())
        provider.set_position_manager(pm)
    return provider, broker, pm


# ---------------------------------------------------------------------------
# EMA construction
# ---------------------------------------------------------------------------

class TestEMA:
    @pytest.mark.asyncio
    async def test_ema_seeds_on_first_bar_and_updates(self):
        provider, broker, _ = make_provider()
        broker.bars = flat_5min(ny(9, 30), 600.0) + flat_5min(ny(9, 35), 606.0)

        await provider._update_market_state(ny(9, 40))

        # EMA(9:35) = 600; EMA(9:40) = 606/3 + 600*2/3 = 602
        assert provider._ema == pytest.approx(602.0)

    @pytest.mark.asyncio
    async def test_ema_resets_on_new_day(self):
        provider, broker, _ = make_provider()
        broker.bars = flat_5min(ny(9, 30), 600.0)
        await provider._update_market_state(ny(9, 35))
        assert provider._ema == pytest.approx(600.0)

        # Next day: state must reset (no bars yet -> backfill returns None)
        next_day = ny(9, 31) + timedelta(days=3)  # Monday
        broker.bars = []
        await provider._update_market_state(next_day)
        assert provider._ema is None
        assert provider._signal_bar is None

    @pytest.mark.asyncio
    async def test_historical_backfill_seeds_ema_after_restart(self):
        provider, broker, _ = make_provider()
        # No live bars before 9:45 (late start); historical has the closes.
        # The 9:45 boundary needs history too: the live builder only covers
        # 9:45 onward, so the 9:40-9:45 window is incomplete.
        broker.historical = {"09:35": 600.0, "09:40": 606.0, "09:45": 608.0}
        broker.bars = flat_5min(ny(9, 45), 610.0)  # live coverage 9:45-9:50

        await provider._update_market_state(ny(9, 50))

        # EMA: 600 -> 602 -> 604 (hist) -> 610/3 + 604*2/3 = 606 (live bar)
        assert provider._ema == pytest.approx(606.0, abs=1e-3)
        assert len(broker.historical_calls) == 3


# ---------------------------------------------------------------------------
# Signal (alert) bar rules
# ---------------------------------------------------------------------------

class TestSignalBar:
    @pytest.mark.asyncio
    async def test_put_alert_when_bar_fully_above_ema(self):
        provider, broker, _ = make_provider()
        # Bar 1 (seeds EMA at 600), bar 2 fully above EMA
        broker.bars = flat_5min(ny(9, 30), 600.0) + one_min_bars(
            ny(9, 35), [(604, 606, 603, 605)] * 5
        )
        await provider._update_market_state(ny(9, 40))

        sig = provider._signal_bar
        assert sig is not None
        assert sig["direction"] == "PUT"
        assert sig["high"] == 606 and sig["low"] == 603

    @pytest.mark.asyncio
    async def test_call_alert_when_bar_fully_below_ema(self):
        provider, broker, _ = make_provider()
        broker.bars = flat_5min(ny(9, 30), 600.0) + one_min_bars(
            ny(9, 35), [(596, 597, 594, 595)] * 5
        )
        await provider._update_market_state(ny(9, 40))

        sig = provider._signal_bar
        assert sig is not None
        assert sig["direction"] == "CALL"
        assert sig["high"] == 597 and sig["low"] == 594

    @pytest.mark.asyncio
    async def test_roll_forward_replaces_signal_bar(self):
        provider, broker, _ = make_provider()
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)   # PUT alert
            + one_min_bars(ny(9, 40), [(605, 608, 604.5, 607)] * 5)  # newer alert
        )
        await provider._update_market_state(ny(9, 45))

        sig = provider._signal_bar
        assert sig["direction"] == "PUT"
        assert sig["high"] == 608 and sig["low"] == 604.5  # rolled forward

    @pytest.mark.asyncio
    async def test_bar_touching_ema_cancels_signal(self):
        provider, broker, _ = make_provider()
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)  # PUT alert
            # next bar straddles the EMA (~601.67) -> cancel
            + one_min_bars(ny(9, 40), [(602, 603, 600, 601)] * 5)
        )
        await provider._update_market_state(ny(9, 45))
        assert provider._signal_bar is None


# ---------------------------------------------------------------------------
# Entry breakout
# ---------------------------------------------------------------------------

class TestEntry:
    @pytest.mark.asyncio
    async def test_put_breakout_emits_long_put_signal(self):
        provider, broker, _ = make_provider()
        cfg = make_config()
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)  # PUT alert
            # 1-min close breaks below signal low (603)
            + one_min_bars(ny(9, 40), [(603, 603.5, 602, 602.5)])
        )
        signals = await provider.poll(cfg, ny(9, 41, 30))

        assert len(signals) == 1
        s = signals[0]
        assert s.contract.right == OptionRight.PUT
        assert s.contract.strike == round(602.5)  # ATM of the breakout close
        assert s.metadata["stop_underlying"] == 606.0
        assert provider._trades_today[cfg.strategy_id] == 1

    @pytest.mark.asyncio
    async def test_no_duplicate_entry_off_same_signal_bar(self):
        provider, broker, _ = make_provider()
        cfg = make_config()
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)
            + one_min_bars(ny(9, 40), [(603, 603.5, 602, 602.5)])
        )
        first = await provider.poll(cfg, ny(9, 41, 30))
        second = await provider.poll(cfg, ny(9, 41, 31))
        assert len(first) == 1
        assert second == []

    @pytest.mark.asyncio
    async def test_max_trades_per_day_blocks_entry(self):
        provider, broker, _ = make_provider()
        cfg = make_config()
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)
            + one_min_bars(ny(9, 40), [(603, 603.5, 602, 602.5)])
        )
        # Let the provider initialize today's state first, THEN exhaust the
        # trade budget (a fresh day reset would wipe the counter).
        await provider._update_market_state(ny(9, 41))
        provider._trades_today[cfg.strategy_id] = 5
        assert await provider.poll(cfg, ny(9, 41, 30)) == []

    @pytest.mark.asyncio
    async def test_no_entry_after_cutoff(self):
        provider, broker, _ = make_provider()
        cfg = make_config()
        provider._day = DAY
        provider._signal_bar = {
            "direction": "PUT", "high": 606.0, "low": 603.0,
            "bar_time": ny(15, 25),
        }
        provider._next_5min_close = ny(16, 5)  # nothing left to process
        broker.bars = one_min_bars(ny(15, 31), [(602, 603, 601, 602)])
        assert await provider.poll(cfg, ny(15, 32)) == []

    @pytest.mark.asyncio
    async def test_no_entry_while_position_open(self):
        provider, broker, pm = make_provider()
        cfg = make_config()
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        broker.bars = (
            flat_5min(ny(9, 30), 600.0)
            + one_min_bars(ny(9, 35), [(604, 606, 603, 605)] * 5)
            + one_min_bars(ny(9, 40), [(603, 603.5, 602, 602.5)])
        )
        assert await provider.poll(cfg, ny(9, 41, 30)) == []


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def bind_ctx(provider, pos, *, direction="CALL", stop=598.0, entry_underlying=601.0,
             entry_price=2.50):
    provider._pos_ctx[pos.position_id] = {
        "strategy_id": pos.strategy_id,
        "direction": direction,
        "stop_underlying": stop,
        "entry_underlying": entry_underlying,
        "entry_price": entry_price,
        "peak_price": entry_price,
        "locked_floor": None,
    }


def prime_day(provider, broker, last_close: float):
    """Set per-day state and the latest 1-min close without full bar history."""
    provider._day = DAY
    provider._next_5min_close = ny(16, 5)  # nothing pending to process
    broker.bars = one_min_bars(ny(10, 0), [(last_close, last_close, last_close, last_close)])


class TestBaseExits:
    @pytest.mark.asyncio
    async def test_underlying_stop_call(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0, entry_underlying=601.0)
        prime_day(provider, broker, last_close=597.5)  # below stop

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == {pos.position_id}

    @pytest.mark.asyncio
    async def test_underlying_stop_put(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id, right=OptionRight.PUT)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="PUT", stop=606.0, entry_underlying=603.0)
        prime_day(provider, broker, last_close=606.5)  # above stop

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == {pos.position_id}

    @pytest.mark.asyncio
    async def test_three_r_target_call(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        # risk = 601 - 598 = 3 -> target = 601 + 9 = 610
        bind_ctx(provider, pos, direction="CALL", stop=598.0, entry_underlying=601.0)
        prime_day(provider, broker, last_close=610.2)

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == {pos.position_id}

    @pytest.mark.asyncio
    async def test_no_exit_inside_range(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0, entry_underlying=601.0)
        prime_day(provider, broker, last_close=604.0)  # between stop and 3R

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == set()

    @pytest.mark.asyncio
    async def test_time_exit_1545(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0, entry_underlying=601.0)
        prime_day(provider, broker, last_close=604.0)

        exits = await provider.collect_exits(cfg, ny(15, 45, 1))
        assert exits == {pos.position_id}


class TestTrailExits:
    def _option_quote(self, broker, pos, mid):
        key = pos.contract.to_quote_symbol()
        broker.option_quotes[key] = QuoteSnapshot(
            symbol=key, bid=mid - 0.05, ask=mid + 0.05, timestamp=datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_no_floor_until_first_20pct_step(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_trail")
        pos = make_position(strategy_id=cfg.strategy_id, entry_price=2.00)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0,
                 entry_underlying=601.0, entry_price=2.00)
        prime_day(provider, broker, last_close=604.0)
        self._option_quote(broker, pos, mid=2.30)  # +15%: below first step

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == set()
        assert provider._pos_ctx[pos.position_id]["locked_floor"] is None
        assert provider._pos_ctx[pos.position_id]["peak_price"] == pytest.approx(2.30)

    @pytest.mark.asyncio
    async def test_floor_locks_and_triggers(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_trail")
        pos = make_position(strategy_id=cfg.strategy_id, entry_price=2.00)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0,
                 entry_underlying=601.0, entry_price=2.00)
        prime_day(provider, broker, last_close=604.0)

        # Peak reaches +45% (2.90): steps=2 -> floor = 2.00 + 2*0.30 = 2.60
        self._option_quote(broker, pos, mid=2.90)
        assert await provider.collect_exits(cfg, ny(10, 1)) == set()

        # Premium falls to the floor -> exit
        self._option_quote(broker, pos, mid=2.55)
        exits = await provider.collect_exits(cfg, ny(10, 2))
        assert exits == {pos.position_id}
        assert provider._pos_ctx[pos.position_id]["locked_floor"] == pytest.approx(2.60)

    @pytest.mark.asyncio
    async def test_underlying_stop_still_applies_in_trail_mode(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_trail")
        pos = make_position(strategy_id=cfg.strategy_id, entry_price=2.00)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos, direction="CALL", stop=598.0,
                 entry_underlying=601.0, entry_price=2.00)
        prime_day(provider, broker, last_close=597.0)  # stop breach
        self._option_quote(broker, pos, mid=1.50)

        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == {pos.position_id}


class TestAdoption:
    @pytest.mark.asyncio
    async def test_adopted_position_gets_fallback_premium_stop(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id, entry_price=2.00)
        pm.positions[pos.position_id] = pos  # no ctx, no pending: restart case
        prime_day(provider, broker, last_close=604.0)

        # Premium 25% below entry -> fallback stop fires
        key = pos.contract.to_quote_symbol()
        broker.option_quotes[key] = QuoteSnapshot(
            symbol=key, bid=1.45, ask=1.55, timestamp=datetime.now(UTC),
        )
        exits = await provider.collect_exits(cfg, ny(10, 1))
        assert exits == {pos.position_id}
        ctx = provider._pos_ctx[pos.position_id]
        assert ctx["stop_underlying"] is None

    @pytest.mark.asyncio
    async def test_pending_ctx_binds_to_new_position(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        provider._pending_ctx[cfg.strategy_id] = {
            "direction": "PUT", "stop_underlying": 606.0,
            "entry_underlying": 603.0, "entry_price": 2.40,
            "peak_price": 2.40, "locked_floor": None,
        }
        pos = make_position(strategy_id=cfg.strategy_id, right=OptionRight.PUT,
                            entry_price=2.45)
        pm.positions[pos.position_id] = pos

        provider._sync_position_contexts(cfg.strategy_id, provider.STRATEGY_PARAMS[cfg.strategy_id])

        ctx = provider._pos_ctx[pos.position_id]
        assert ctx["stop_underlying"] == 606.0
        # entry premium refined to the actual average fill
        assert ctx["entry_price"] == pytest.approx(2.45)
        assert provider._pending_ctx.get(cfg.strategy_id) is None

    @pytest.mark.asyncio
    async def test_ctx_dropped_when_position_closes(self):
        provider, broker, pm = make_provider()
        cfg = make_config("xsp_5ema_base")
        pos = make_position(strategy_id=cfg.strategy_id)
        pm.positions[pos.position_id] = pos
        bind_ctx(provider, pos)

        pos.status = PositionStatus.CLOSED
        provider._sync_position_contexts(cfg.strategy_id, provider.STRATEGY_PARAMS[cfg.strategy_id])
        assert pos.position_id not in provider._pos_ctx
