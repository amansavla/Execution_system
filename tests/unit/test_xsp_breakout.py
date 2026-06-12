import pytest
from datetime import datetime, UTC, date, time
from zoneinfo import ZoneInfo
from typing import Optional
from uuid import UUID

from src.core.enums import OptionRight, SignalDirection
from src.core.models import OptionContract, QuoteSnapshot, Position, PositionStatus, OrderSide
from src.core.config import StrategyConfig, StrategyEntryConfig, StrategyExitConfig
from src.strategies.xsp_breakout import XSPBreakoutStrategyProvider, STRATEGY_PARAMS
from src.broker.interface import BrokerClient

class StubBroker(BrokerClient):
    def __init__(self) -> None:
        self.connected = True
        self.historical_close = 500.0
        self.historical_queries = []
        self.quote_queries = []
        self.mock_quote = QuoteSnapshot(
            symbol="XSP",
            bid=501.0,
            ask=501.0,
            last=501.0,
            timestamp=datetime.now(UTC),
        )

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def is_connected(self) -> bool:
        return self.connected

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        self.quote_queries.append(symbols)
        if "XSP" in symbols:
            return {"XSP": self.mock_quote}
        # Serve a fixed option quote (mid=2.5) for any option symbol queried
        # by the dynamic-sizing logic.
        return {
            sym: QuoteSnapshot(
                symbol=sym, bid=2.4, ask=2.6, last=2.5, timestamp=datetime.now(UTC)
            )
            for sym in symbols
        }

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        self.historical_queries.append((symbol, end_time))
        return self.historical_close

    async def place_order(self, order_plan):
        pass

    async def cancel_order(self, broker_order_id: str) -> bool:
        return True

    async def get_open_orders(self):
        return []

    async def get_completed_orders(self):
        return []

    async def get_positions(self):
        return []

    async def get_account_state(self):
        class _Acct:
            net_liquidation = 100_000.0
        return _Acct()

    def register_quote_callback(self, callback) -> None:
        pass

    def register_order_callback(self, callback) -> None:
        pass

    def register_fill_callback(self, callback) -> None:
        pass


def test_option_contract_to_quote_symbol() -> None:
    # Test integer strike
    contract_c = OptionContract(
        symbol="XSP",
        expiry="20260521",
        strike=510.0,
        right=OptionRight.CALL,
    )
    assert contract_c.to_quote_symbol() == "XSP 20260521 510 C"

    contract_p = OptionContract(
        symbol="XSP",
        expiry="20260521",
        strike=510.0,
        right=OptionRight.PUT,
    )
    assert contract_p.to_quote_symbol() == "XSP 20260521 510 P"

    # Test float strike
    contract_float = OptionContract(
        symbol="XSP",
        expiry="20260521",
        strike=510.5,
        right=OptionRight.CALL,
    )
    assert contract_float.to_quote_symbol() == "XSP 20260521 510.5 C"


@pytest.mark.asyncio
async def test_xsp_breakout_poll_timing() -> None:
    broker = StubBroker()
    provider = XSPBreakoutStrategyProvider(broker)
    
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=1),
    )
    
    # 9:59 AM New York time (before scan start)
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(9, 59), tzinfo=tz_ny).astimezone(UTC)
    
    signals = await provider.poll(config, poll_time)
    assert signals == []
    assert len(broker.quote_queries) == 0


@pytest.mark.asyncio
async def test_xsp_breakout_poll_trigger_long() -> None:
    broker = StubBroker()
    broker.historical_close = 500.0
    # +0.3% move: 500.0 * 1.003 = 501.5
    broker.mock_quote = QuoteSnapshot(
        symbol="XSP",
        bid=501.5,
        ask=501.5,
        timestamp=datetime.now(UTC)
    )
    
    provider = XSPBreakoutStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=3),
    )
    
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)
    
    signals = await provider.poll(config, poll_time)
    assert len(signals) == 1
    sig = signals[0]
    
    assert sig.strategy_id == "xsp_0dte_1000"
    assert sig.direction == SignalDirection.LONG
    assert sig.contract.symbol == "XSP"
    assert sig.contract.right == OptionRight.CALL
    assert sig.contract.strike == 502.0  # round(501.5)
    # Dynamic sizing (backtest formula): qty = int(100000 * 0.01 / (2.5 * 100)) = 4,
    # capped by max_contracts=3
    assert sig.requested_quantity == 3
    assert sig.limit_price == 2.5
    assert sig.metadata["option_mid_at_signal"] == 2.5
    assert sig.metadata["net_liquidation"] == 100_000.0
    assert sig.metadata["trigger_type"] == "breakout_long"
    assert sig.metadata["reference_price"] == 500.0
    assert sig.metadata["underlying_price_at_entry"] == 501.5
    assert sig.metadata["atm_strike"] == 502.0


@pytest.mark.asyncio
async def test_xsp_breakout_poll_trigger_short() -> None:
    broker = StubBroker()
    broker.historical_close = 500.0
    # -0.3% move: 500.0 * 0.997 = 498.5
    broker.mock_quote = QuoteSnapshot(
        symbol="XSP",
        bid=498.5,
        ask=498.5,
        timestamp=datetime.now(UTC)
    )
    
    provider = XSPBreakoutStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=2),
    )
    
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)
    
    signals = await provider.poll(config, poll_time)
    assert len(signals) == 1
    sig = signals[0]
    
    assert sig.contract.right == OptionRight.PUT
    assert sig.contract.strike == 498.0  # round(498.5) under banker's rounding
    assert sig.metadata["trigger_type"] == "breakout_short"
    assert sig.metadata["reference_price"] == 500.0
    assert sig.metadata["underlying_price_at_entry"] == 498.5


@pytest.mark.asyncio
async def test_xsp_breakout_poll_no_trigger() -> None:
    broker = StubBroker()
    broker.historical_close = 500.0
    # +0.1% move: 500.0 * 1.001 = 500.5
    broker.mock_quote = QuoteSnapshot(
        symbol="XSP",
        bid=500.5,
        ask=500.5,
        timestamp=datetime.now(UTC)
    )
    
    provider = XSPBreakoutStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=1),
    )
    
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)
    
    signals = await provider.poll(config, poll_time)
    assert signals == []


@pytest.mark.asyncio
async def test_xsp_breakout_poll_duplicate_prevention() -> None:
    broker = StubBroker()
    broker.historical_close = 500.0
    broker.mock_quote = QuoteSnapshot(
        symbol="XSP",
        bid=501.5,
        ask=501.5,
        timestamp=datetime.now(UTC)
    )
    
    provider = XSPBreakoutStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=1),
    )
    
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)
    
    # First poll triggers signal
    signals1 = await provider.poll(config, poll_time)
    assert len(signals1) == 1
    
    # Second poll on the same day does NOT trigger
    signals2 = await provider.poll(config, poll_time)
    assert signals2 == []


@pytest.mark.asyncio
async def test_xsp_breakout_historical_fallback_failure() -> None:
    broker = StubBroker()
    broker.historical_close = None  # Failed fetch
    
    provider = XSPBreakoutStrategyProvider(broker)
    config = StrategyConfig(
        strategy_id="xsp_0dte_1000",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="test", max_contracts=1),
    )
    
    tz_ny = ZoneInfo("America/New_York")
    poll_time = datetime.combine(date(2026, 5, 21), time(10, 1), tzinfo=tz_ny).astimezone(UTC)
    
    signals = await provider.poll(config, poll_time)
    assert signals == []
    # Assert historical fetch was actually queried
    assert len(broker.historical_queries) == 1
    
    # Subsequent calls on the same day should skip the query to prevent spamming
    signals2 = await provider.poll(config, poll_time)
    assert signals2 == []
    assert len(broker.historical_queries) == 1
