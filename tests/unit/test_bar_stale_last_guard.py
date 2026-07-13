from datetime import UTC, datetime

from src.broker.ibkr_broker import IBKRBrokerClient
from src.marketdata.bars import BarBuilder


# _on_pending_ticker only touches bar_builder, _last_bar_tick_price, and
# _quote_callbacks — a bare stand-in avoids IBKRBrokerClient's IB-connection
# constructor (mirrors the pattern used for ExecutionRunner's entry-window
# tests).
class _BrokerStub:
    def __init__(self) -> None:
        self.bar_builder = BarBuilder()
        self._last_bar_tick_price: dict[str, float] = {}
        self._quote_callbacks: list = []

    _on_pending_ticker = IBKRBrokerClient._on_pending_ticker
    _internal_symbol_for_ib_contract = IBKRBrokerClient._internal_symbol_for_ib_contract
    _from_ib_contract = IBKRBrokerClient._from_ib_contract


class _FakeContract:
    def __init__(self, symbol="XSP", secType="OPT", right="C", strike=742.0,
                 lastTradeDateOrContractMonth="20260702"):
        self.symbol = symbol
        self.secType = secType
        self.right = right
        self.strike = strike
        self.lastTradeDateOrContractMonth = lastTradeDateOrContractMonth


class _FakeTicker:
    def __init__(self, contract, bid=None, ask=None, last=None, volume=None,
                 time=None, close=None):
        self.contract = contract
        self.bid = bid
        self.ask = ask
        self.last = last
        self.volume = volume
        self.time = time
        self.close = close
        self.modelGreeks = None


def test_repeated_stale_last_does_not_repollute_bar_high() -> None:
    # Regression 2026-07-02: a frozen `last` from before entry got re-fed
    # into the bar on every bid/ask-only tick, keeping bar.high pinned to a
    # stale price that was never actually tradeable — causing a false
    # stop-loss trigger on XSP 742 CALL. Fixed by only feeding on_tick when
    # `last` has genuinely changed since the previous observation.
    broker = _BrokerStub()
    contract = _FakeContract()
    ts = datetime(2026, 7, 2, 17, 0, 30, tzinfo=UTC)

    # First observation: last=4.06 (the stale print) folds in once.
    broker._on_pending_ticker(_FakeTicker(contract, bid=3.15, ask=3.42, last=4.06, time=ts))
    # Many subsequent bid/ask-only ticks with the SAME stale last — must
    # NOT re-touch bar.high even though pendingTickersEvent fires each time.
    for bid, ask in [(3.03, 3.62), (2.82, 3.05), (2.64, 2.86), (2.75, 2.97)]:
        broker._on_pending_ticker(_FakeTicker(contract, bid=bid, ask=ask, last=4.06, time=ts))

    bar = broker.bar_builder.get_latest_completed_bar("XSP 20260702 742 C", now=ts)
    working = broker.bar_builder._working.get("XSP 20260702 742 C")
    the_bar = bar or working
    assert the_bar is not None
    assert the_bar.tick_count == 1, "stale last repeated 5x should only feed the bar once"
    assert the_bar.high == 4.06


def test_genuinely_new_last_still_updates_bar() -> None:
    broker = _BrokerStub()
    contract = _FakeContract()
    ts = datetime(2026, 7, 2, 17, 0, 30, tzinfo=UTC)

    broker._on_pending_ticker(_FakeTicker(contract, bid=3.15, ask=3.42, last=4.06, time=ts))
    broker._on_pending_ticker(_FakeTicker(contract, bid=2.7, ask=2.9, last=2.80, time=ts))

    working = broker.bar_builder._working.get("XSP 20260702 742 C")
    assert working.tick_count == 2
    assert working.low == 2.80
    assert working.close == 2.80


def test_index_mid_fallback_still_dedups_on_unchanged_mid() -> None:
    broker = _BrokerStub()
    contract = _FakeContract(symbol="XSP", secType="IND", right="", strike=0.0,
                              lastTradeDateOrContractMonth="")
    ts = datetime(2026, 7, 2, 17, 0, 30, tzinfo=UTC)

    broker._on_pending_ticker(_FakeTicker(contract, bid=749.5, ask=749.6, last=None, time=ts))
    broker._on_pending_ticker(_FakeTicker(contract, bid=749.5, ask=749.6, last=None, time=ts))

    working = broker.bar_builder._working.get("XSP")
    assert working.tick_count == 1
