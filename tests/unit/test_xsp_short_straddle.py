import pytest
from datetime import datetime, UTC, date, time
from zoneinfo import ZoneInfo
from typing import Optional
from uuid import UUID

from src.core.enums import OptionRight, SignalDirection, OrderSide
from src.core.models import OptionContract, QuoteSnapshot, AccountState, StrategySignal
from src.core.config import StrategyConfig, StrategyEntryConfig, StrategyExitConfig
from src.strategies.xsp_short_straddle import XSPShortStraddleStrategyProvider, parse_entry_time
from src.strategies.composite import CompositeStrategyProvider
from src.broker.interface import BrokerClient


class StubBroker(BrokerClient):
    def __init__(self, underlying_price: float = 500.0) -> None:
        self.connected = True
        self.underlying_price = underlying_price
        self.quote_queries = []
        self.account_state_calls = 0

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return self.connected

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        self.quote_queries.append(symbols)
        result = {}
        now = datetime.now(UTC)

        for sym in symbols:
            if sym == "XSP":
                result["XSP"] = QuoteSnapshot(
                    symbol="XSP",
                    bid=self.underlying_price - 0.05,
                    ask=self.underlying_price + 0.05,
                    last=self.underlying_price,
                    timestamp=now
                )
            else:
                # Option symbol format: "XSP YYYYMMDD strike C/P"
                parts = sym.split()
                if len(parts) >= 4:
                    try:
                        strike = float(parts[2])
                    except ValueError:
                        strike = 500.0
                    right_char = parts[3]
                    
                    dist = abs(strike - self.underlying_price)
                    time_value = max(0.05, 3.0 - 0.4 * dist)
                    
                    if right_char.upper() == "C":
                        if strike < self.underlying_price:
                            # Call ITM: intrinsic + time value
                            mid = (self.underlying_price - strike) + time_value
                        else:
                            # Call OTM: time value only
                            mid = time_value
                    else:
                        if strike > self.underlying_price:
                            # Put ITM: intrinsic + time value
                            mid = (strike - self.underlying_price) + time_value
                        else:
                            # Put OTM: time value only
                            mid = time_value

                    result[sym] = QuoteSnapshot(
                        symbol=sym,
                        bid=mid - 0.02,
                        ask=mid + 0.02,
                        timestamp=now
                    )
        return result

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        return self.underlying_price

    async def place_order(self, order_plan):
        pass

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_open_orders(self):
        return []

    async def get_positions(self):
        return []

    async def get_account_state(self) -> AccountState:
        self.account_state_calls += 1
        return AccountState(
            account_id="MOCK_ACC_123",
            net_liquidation=100000.0,
            available_funds=80000.0,
            buying_power=400000.0,
            timestamp=datetime.now(UTC),
        )

    def register_quote_callback(self, callback) -> None:
        pass

    def register_order_callback(self, callback) -> None:
        pass

    def register_fill_callback(self, callback) -> None:
        pass


def test_parse_entry_time() -> None:
    assert parse_entry_time("xsp_straddle_1000_20") == (10, 0)
    assert parse_entry_time("xsp_straddle_0945") == (9, 45)
    assert parse_entry_time("xsp_straddle_invalid") is None


@pytest.mark.asyncio
async def test_xsp_short_straddle_poll_timing() -> None:
    broker = StubBroker()
    provider = XSPShortStraddleStrategyProvider(broker)

    config = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=5),
        leverage=12.0,
        position_sizing_pct=0.025
    )

    tz_ny = ZoneInfo("America/New_York")
    # 9:59 AM (before start time)
    poll_time = datetime.combine(date(2026, 5, 21), time(9, 59), tzinfo=tz_ny).astimezone(UTC)

    signals = await provider.poll(config, poll_time)
    assert signals == []
    assert len(broker.quote_queries) == 0


@pytest.mark.asyncio
async def test_xsp_short_straddle_poll_trigger() -> None:
    # U = 500.0, leverage = 12, ps_pct = 0.025
    # target_premium = U * 2 * 0.025 / 12 = 500 * 0.05 / 12 = 2.0833
    # option premium model: time_value = max(0.05, 3.0 - 0.4 * dist)
    # Since selected strike is OTM, mid = time_value.
    # We want mid closest to 2.0833:
    # Solve 3.0 - 0.4 * dist = 2.0833 -> 0.4 * dist = 0.9167 -> dist = 2.29
    # Option strikes:
    # dist = 2 -> mid = 3.0 - 0.8 = 2.2 (diff to target_premium = 0.1167)
    # dist = 3 -> mid = 3.0 - 1.2 = 1.8 (diff to target_premium = 0.2833)
    # So it should select dist = 2:
    # Call strike: 502.0
    # Put strike: 498.0
    broker = StubBroker(underlying_price=500.0)
    provider = XSPShortStraddleStrategyProvider(broker)

    config = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=10),
        exit=StrategyExitConfig(stop_loss_pct=20, time_exit_utc="15:30"),
        leverage=12.0,
        position_sizing_pct=0.025
    )

    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)

    signals = await provider.poll(config, poll_time)
    assert len(signals) == 2
    
    call_sig = next(s for s in signals if s.metadata["leg"] == "call")
    put_sig = next(s for s in signals if s.metadata["leg"] == "put")

    assert call_sig.strategy_id == "xsp_straddle_1000_20"
    assert call_sig.direction == SignalDirection.SHORT
    assert call_sig.contract.symbol == "XSP"
    assert call_sig.contract.right == OptionRight.CALL
    assert call_sig.contract.strike == 502.0
    
    assert put_sig.strategy_id == "xsp_straddle_1000_20"
    assert put_sig.direction == SignalDirection.SHORT
    assert put_sig.contract.symbol == "XSP"
    assert put_sig.contract.right == OptionRight.PUT
    assert put_sig.contract.strike == 498.0

    # Test per-leg dynamic quantity calculation (exact backtest formula):
    # margin = 500 * 100 * 2 / 12 = 8333.33
    # leg mid = 2.2 -> upside = 220 / 8333.33 = 0.0264
    # ps_leg = min(0.0264, 0.025) = 0.025
    # qty = int(100000 * 0.025 / 220) = int(11.36) = 11 -> capped at max_contracts=10
    assert call_sig.requested_quantity == 10
    assert put_sig.requested_quantity == 10


@pytest.mark.asyncio
async def test_xsp_short_straddle_duplicate_prevention() -> None:
    broker = StubBroker()
    provider = XSPShortStraddleStrategyProvider(broker)

    config = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=5),
        leverage=12.0,
        position_sizing_pct=0.025
    )

    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)

    # First poll triggers signals
    signals1 = await provider.poll(config, poll_time)
    assert len(signals1) == 2

    # One-trade-per-day is enforced by an ACTUAL position existing today,
    # NOT by the signal having been emitted (emit-then-reject must NOT burn
    # the day's slot — regression 2026-06-15). Simulate the fill by
    # registering an open position for this strategy today.
    from src.portfolio.position_manager import PositionManager
    from src.storage.event_log import EventStore
    from src.core.models import Position
    from src.core.enums import PositionStatus
    from uuid import uuid4

    pm = PositionManager(EventStore())
    pm.positions[uuid4()] = Position(
        position_id=uuid4(),
        strategy_id="xsp_straddle_1000_20",
        contract=OptionContract(symbol="XSP", expiry="20260521", strike=500.0,
                                right=OptionRight.CALL, multiplier=100),
        side=OrderSide.SELL,
        quantity=2,
        average_entry_price=2.5,
        status=PositionStatus.OPEN,
        entry_order_id=uuid4(),
        entry_time=poll_time,
    )
    provider.set_position_manager(pm)

    # Second poll on the same day does NOT trigger — a position exists.
    signals2 = await provider.poll(config, poll_time)
    assert signals2 == []


@pytest.mark.asyncio
async def test_xsp_short_straddle_rejected_signal_does_not_burn_slot() -> None:
    """A signal that never becomes a position must NOT block later polls.

    Regression: the provider used to mark _traded_today on emit, so a
    risk-rejected signal (no position created) locked the strategy out for
    the rest of the day. With no position registered, a second poll must
    still emit.
    """
    broker = StubBroker()
    provider = XSPShortStraddleStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=5),
        leverage=12.0,
        position_sizing_pct=0.025,
    )
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)

    signals1 = await provider.poll(config, poll_time)
    assert len(signals1) == 2
    # No position was created (signal was "rejected") -> still emits.
    signals2 = await provider.poll(config, poll_time)
    assert len(signals2) == 2


@pytest.mark.asyncio
async def test_composite_strategy_provider() -> None:
    broker = StubBroker()
    breakout_provider = XSPShortStraddleStrategyProvider(broker)  # reuse as dummy
    straddle_provider = XSPShortStraddleStrategyProvider(broker)

    composite = CompositeStrategyProvider({
        "xsp_breakout": breakout_provider,
        "xsp_short_straddle": straddle_provider,
    })

    config_straddle = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=5),
        leverage=12.0,
        position_sizing_pct=0.025
    )

    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)

    # Poll with valid source
    signals = await composite.poll(config_straddle, poll_time)
    assert len(signals) == 2

    # Poll with invalid/unregistered source
    config_invalid = StrategyConfig(
        strategy_id="xsp_invalid",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="unknown_source", max_contracts=5)
    )
    signals_invalid = await composite.poll(config_invalid, poll_time)
    assert signals_invalid == []


@pytest.mark.asyncio
async def test_xsp_short_straddle_allow_reentry() -> None:
    broker = StubBroker()
    provider = XSPShortStraddleStrategyProvider(broker)

    config = StrategyConfig(
        strategy_id="xsp_straddle_1000_20",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=5),
        leverage=12.0,
        position_sizing_pct=0.025,
        allow_reentry=True
    )

    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)

    # First poll triggers signals
    signals1 = await provider.poll(config, poll_time)
    assert len(signals1) == 2

    # Second poll with allow_reentry=True STILL triggers signals!
    signals2 = await provider.poll(config, poll_time)
    assert len(signals2) == 2
