from datetime import UTC, datetime
from uuid import uuid4

from src.core.enums import OptionRight, OrderSide, PositionStatus
from src.core.models import OptionContract, Position
from src.storage.position_store import PositionStore


def _make_position(strategy_id: str, strike: float, right: OptionRight, qty: int) -> Position:
    return Position(
        position_id=uuid4(),
        strategy_id=strategy_id,
        contract=OptionContract(symbol="XSP", expiry="20260701", strike=strike,
                                 right=right, multiplier=100),
        side=OrderSide.SELL,
        quantity=qty,
        average_entry_price=2.5,
        status=PositionStatus.OPEN,
        entry_order_id=uuid4(),
        entry_time=datetime.now(UTC),
    )


def test_find_open_attribution_returns_only_most_recent() -> None:
    store = PositionStore(":memory:")
    p1 = _make_position("xsp_straddle_1100_40", 747.0, OptionRight.CALL, 6)
    p2 = _make_position("xsp_straddle_1300_30", 747.0, OptionRight.CALL, 6)
    store.upsert_position(p1)
    store.upsert_position(p2)

    result = store.find_open_attribution("XSP", "20260701", 747.0, "CALL")
    assert result is not None
    assert result["strategy_id"] == "xsp_straddle_1300_30"  # most recently updated


def test_find_all_open_attributions_returns_every_matching_strategy() -> None:
    # Regression: two straddles independently picking the identical strike
    # (real event, 2026-07-01 — xsp_straddle_1100_40 and xsp_straddle_1300_30
    # both sold XSP 747 CALL). On restart, seeding must recover BOTH rows,
    # not just the most-recently-updated one, or one strategy's exposure
    # gets silently dropped/double-counted onto the other.
    store = PositionStore(":memory:")
    p1 = _make_position("xsp_straddle_1100_40", 747.0, OptionRight.CALL, 6)
    p2 = _make_position("xsp_straddle_1300_30", 747.0, OptionRight.CALL, 6)
    store.upsert_position(p1)
    store.upsert_position(p2)

    results = store.find_all_open_attributions("XSP", "20260701", 747.0, "CALL")
    assert len(results) == 2
    strategy_ids = {r["strategy_id"] for r in results}
    assert strategy_ids == {"xsp_straddle_1100_40", "xsp_straddle_1300_30"}
    assert sum(r["quantity"] for r in results) == 12


def test_find_all_open_attributions_excludes_closed_rows() -> None:
    store = PositionStore(":memory:")
    p1 = _make_position("xsp_straddle_1100_40", 747.0, OptionRight.CALL, 6)
    store.upsert_position(p1)
    p1.status = PositionStatus.CLOSED
    store.upsert_position(p1, close_reason="time_exit")

    results = store.find_all_open_attributions("XSP", "20260701", 747.0, "CALL")
    assert results == []


def test_find_all_open_attributions_no_match_returns_empty_list() -> None:
    store = PositionStore(":memory:")
    results = store.find_all_open_attributions("XSP", "20260701", 999.0, "CALL")
    assert results == []
