"""Unit tests for OptionContractSelector.

Covers:
- Selection by right (CALL or PUT)
- Selection by DTE target (0DTE, 1DTE)
- Delta filtering and choosing closest delta match
- Strike/moneyness filtering
- Min bid, spread limits, and freshness checks
- Detailed rejection reasons reporting
- Boundary isolation (no BrokerClient or order execution imports)
"""

import inspect
from datetime import UTC, datetime, timedelta
import pytest

from src.core.enums import OptionRight
from src.core.models import OptionContract, QuoteSnapshot
from src.marketdata.contract_selector import OptionContractSelector


def _make_contract(
    symbol: str,
    strike: float,
    right: OptionRight,
    expiry: str,
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        strike=strike,
        right=right,
        expiry=expiry,
        multiplier=100,
    )


class TestOptionContractSelector:
    @pytest.fixture
    def selector(self) -> OptionContractSelector:
        # 60s max age, 10% max spread, 0.05 min bid
        return OptionContractSelector(max_age_seconds=60.0, max_spread_pct=10.0, min_bid=0.05)

    def test_selects_by_right_and_dte(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry_0dte = "20260520"
        expiry_1dte = "20260521"

        c_call_0dte = _make_contract("CALL_0", 5200.0, OptionRight.CALL, expiry_0dte)
        c_put_0dte = _make_contract("PUT_0", 5200.0, OptionRight.PUT, expiry_0dte)
        c_call_1dte = _make_contract("CALL_1", 5200.0, OptionRight.CALL, expiry_1dte)

        contracts = [c_call_0dte, c_put_0dte, c_call_1dte]

        quotes = {
            "CALL_0": QuoteSnapshot(symbol="CALL_0", bid=3.00, ask=3.10, timestamp=current_time),
            "PUT_0": QuoteSnapshot(symbol="PUT_0", bid=3.00, ask=3.10, timestamp=current_time),
            "CALL_1": QuoteSnapshot(symbol="CALL_1", bid=4.00, ask=4.15, timestamp=current_time),
        }

        # 1. Target CALL 0DTE
        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time
        )
        assert sel == c_call_0dte
        assert "CALL_0" not in reasons
        assert "PUT_0" in reasons
        assert "CALL_1" in reasons

        # 2. Target PUT 0DTE
        sel_put, _ = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.PUT, dte_target=0, current_time=current_time
        )
        assert sel_put == c_put_0dte

        # 3. Target CALL 1DTE
        sel_1dte, _ = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=1, current_time=current_time
        )
        assert sel_1dte == c_call_1dte

    def test_delta_filtering_and_closest_sorting(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c1 = _make_contract("C1", 5210.0, OptionRight.CALL, expiry)
        c2 = _make_contract("C2", 5220.0, OptionRight.CALL, expiry)
        c3 = _make_contract("C3", 5230.0, OptionRight.CALL, expiry)
        contracts = [c1, c2, c3]

        quotes = {
            "C1": QuoteSnapshot(symbol="C1", bid=2.50, ask=2.60, delta=0.45, timestamp=current_time),
            "C2": QuoteSnapshot(symbol="C2", bid=1.80, ask=1.90, delta=0.32, timestamp=current_time),
            "C3": QuoteSnapshot(symbol="C3", bid=1.10, ask=1.15, delta=0.21, timestamp=current_time),
        }

        # Target delta = 0.30 -> C2 (0.32) is closest
        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time, target_delta=0.30
        )
        assert sel == c2

        # Target delta = 0.20 -> C3 (0.21) is closest
        sel2, _ = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time, target_delta=0.20
        )
        assert sel2 == c3

    def test_moneyness_strike_distance_filtering(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c_close = _make_contract("CLOSE", 5210.0, OptionRight.CALL, expiry)  # 10 strike diff = ~0.19%
        c_far = _make_contract("FAR", 5500.0, OptionRight.CALL, expiry)    # 300 strike diff = ~5.77%
        contracts = [c_close, c_far]

        quotes = {
            "CLOSE": QuoteSnapshot(symbol="CLOSE", bid=2.50, ask=2.60, timestamp=current_time),
            "FAR": QuoteSnapshot(symbol="FAR", bid=0.10, ask=0.11, timestamp=current_time),
        }

        # Max strike distance 1% -> only CLOSE is eligible
        sel, reasons = selector.select_contract(
            contracts,
            quotes,
            underlying_price=5200.0,
            right=OptionRight.CALL,
            dte_target=0,
            current_time=current_time,
            max_strike_distance_pct=0.01,
        )
        assert sel == c_close
        assert "FAR" in reasons
        assert any("strike_out_of_bounds" in r for r in reasons["FAR"])

    def test_stale_quote_rejection(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c = _make_contract("C", 5200.0, OptionRight.CALL, expiry)
        contracts = [c]

        # Quote timestamp is 70 seconds ago (max_age is 60)
        stale_time = current_time - timedelta(seconds=70)
        quotes = {
            "C": QuoteSnapshot(symbol="C", bid=2.50, ask=2.60, timestamp=stale_time),
        }

        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time
        )
        assert sel is None
        assert "C" in reasons
        assert any("quote_stale" in r for r in reasons["C"])

    def test_missing_bid_or_ask_rejection(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c1 = _make_contract("C1", 5200.0, OptionRight.CALL, expiry)
        c2 = _make_contract("C2", 5210.0, OptionRight.CALL, expiry)
        contracts = [c1, c2]

        quotes = {
            "C1": QuoteSnapshot(symbol="C1", bid=None, ask=2.60, timestamp=current_time),
            "C2": QuoteSnapshot(symbol="C2", bid=2.50, ask=None, timestamp=current_time),
        }

        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time
        )
        assert sel is None
        assert "C1" in reasons
        assert "C2" in reasons
        assert any("missing_bid_or_ask" in r for r in reasons["C1"])
        assert any("missing_bid_or_ask" in r for r in reasons["C2"])

    def test_wide_spread_rejection(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c = _make_contract("C", 5200.0, OptionRight.CALL, expiry)
        contracts = [c]

        # Bid 1.00, Ask 1.20 -> mid = 1.10. Spread = 0.20 / 1.10 = 18.18% (max spread is 10%)
        quotes = {
            "C": QuoteSnapshot(symbol="C", bid=1.00, ask=1.20, timestamp=current_time),
        }

        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time
        )
        assert sel is None
        assert "C" in reasons
        assert any("spread_too_wide" in r for r in reasons["C"])

    def test_min_bid_rejection(self, selector):
        current_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
        expiry = "20260520"

        c = _make_contract("C", 5200.0, OptionRight.CALL, expiry)
        contracts = [c]

        # Bid 0.03 (min_bid is 0.05)
        quotes = {
            "C": QuoteSnapshot(symbol="C", bid=0.03, ask=0.04, timestamp=current_time),
        }

        sel, reasons = selector.select_contract(
            contracts, quotes, underlying_price=5200.0, right=OptionRight.CALL, dte_target=0, current_time=current_time
        )
        assert sel is None
        assert "C" in reasons
        assert any("bid_below_threshold" in r for r in reasons["C"])


def test_contract_selector_boundary_isolation():
    """Verify OptionContractSelector does not import BrokerClient or OrderManager."""
    import src.marketdata.contract_selector as cs
    source = inspect.getsource(cs)

    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "broker" not in stripped.lower(), (
                "Forbidden BrokerClient import in contract_selector.py"
            )
            assert "order_manager" not in stripped.lower(), (
                "Forbidden OrderManager import in contract_selector.py"
            )
