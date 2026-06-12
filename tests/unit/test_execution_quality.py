"""Unit tests for execution quality analytics and realized PnL calculations."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
from uuid import uuid4

import pytest

from src.analytics.execution_quality import (
    compute_execution_quality,
    compute_realized_pnl,
    normalize_events,
)
from src.core.enums import OptionRight, OrderSide, OrderStatus
from src.core.models import OptionContract


def test_normalize_events_conversion() -> None:
    """Test that both raw dictionaries and EventRecord objects normalize correctly."""
    from src.storage.repositories import EventRecord

    record_dt = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)
    dict_dt = datetime(2026, 5, 20, 10, 5, 0, tzinfo=UTC)

    raw_record = EventRecord(
        event_id="rec-1",
        event_type="order_event",
        timestamp=record_dt.isoformat(),
        strategy_id="strat_a",
        payload={"order_id": "uuid-1", "new_status": "NEW"},
    )

    raw_dict = {
        "type": "fill_event",
        "timestamp": dict_dt.isoformat(),
        "strategy_id": "strat_a",
        "data": {"order_id": "uuid-1", "fill_price": 10.5, "filled_quantity": 5},
    }

    normalized = normalize_events([raw_dict, raw_record])
    assert len(normalized) == 2
    # Check sorting order by timestamp (rec-1 is first)
    assert normalized[0]["type"] == "order_event"
    assert normalized[0]["strategy_id"] == "strat_a"
    assert normalized[1]["type"] == "fill_event"
    assert normalized[1]["timestamp"] == dict_dt


def test_compute_execution_quality_slippage() -> None:
    """Test slippage, fill rate, and execution cost calculations."""
    order_id = str(uuid4())
    contract = OptionContract(
        symbol="SPY",
        expiry="20260520",
        strike=500.0,
        right=OptionRight.CALL,
    )

    # 1. Quote Snapshot (bid=4.9, ask=5.1 -> mid=5.0)
    # 2. Order Submitted (BUY, 10 contracts)
    # 3. Fill at 5.05 (slippage = 5.05 - 5.0 = +0.05, cost = 0.05 * 10 = 0.5)
    # 4. Completed
    base_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)

    events = [
        {
            "type": "quote_snapshot",
            "timestamp": base_time - timedelta(seconds=10),
            "data": {"symbol": "SPY", "bid": 4.90, "ask": 5.10},
        },
        {
            "type": "order_event",
            "timestamp": base_time,
            "data": {
                "order_id": order_id,
                "new_status": OrderStatus.SUBMITTED,
                "side": OrderSide.BUY,
                "strategy_id": "test_strat",
                "contract": contract.model_dump(),
                "limit_price": 5.10,
                "quantity": 10,
            },
        },
        {
            "type": "fill_event",
            "timestamp": base_time + timedelta(seconds=2),
            "data": {
                "order_id": order_id,
                "fill_price": 5.05,
                "filled_quantity": 10,
                "side": OrderSide.BUY,
                "strategy_id": "test_strat",
                "contract": contract.model_dump(),
            },
        },
        {
            "type": "order_event",
            "timestamp": base_time + timedelta(seconds=3),
            "data": {
                "order_id": order_id,
                "new_status": OrderStatus.FILLED,
            },
        },
    ]

    normalized = normalize_events(events)
    metrics = compute_execution_quality(normalized)

    assert metrics["total_orders"] == 1
    assert metrics["fill_rate"] == 1.0
    assert metrics["rejection_count"] == 0
    assert pytest.approx(metrics["avg_slippage"]) == 0.05
    assert pytest.approx(metrics["total_slippage_cost"]) == 0.5
    assert pytest.approx(metrics["execution_cost_by_strategy"]["test_strat"]) == 0.5
    assert pytest.approx(metrics["execution_cost_by_underlying"]["SPY"]) == 0.5
    assert metrics["avg_time_to_fill_seconds"] == 3.0


def test_compute_realized_pnl_fifo() -> None:
    """Test realized PnL calculations using FIFO matching for long and short cycles."""
    contract = OptionContract(
        symbol="SPY",
        expiry="20260520",
        strike=500.0,
        right=OptionRight.CALL,
    )
    base_time = datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC)

    # Sequence of fills:
    # BUY 10 contracts @ $2.00
    # SELL 6 contracts @ $2.50  -> PnL = (2.50 - 2.00) * 6 = +$3.00
    # SELL 4 contracts @ $2.30  -> PnL = (2.30 - 2.00) * 4 = +$1.20
    # Total PnL = +$4.20
    events = [
        {
            "type": "fill_event",
            "timestamp": base_time,
            "data": {
                "strategy_id": "strat_pnl",
                "contract": contract.model_dump(),
                "side": OrderSide.BUY,
                "filled_quantity": 10,
                "fill_price": 2.00,
            },
        },
        {
            "type": "fill_event",
            "timestamp": base_time + timedelta(seconds=10),
            "data": {
                "strategy_id": "strat_pnl",
                "contract": contract.model_dump(),
                "side": OrderSide.SELL,
                "filled_quantity": 6,
                "fill_price": 2.50,
            },
        },
        {
            "type": "fill_event",
            "timestamp": base_time + timedelta(seconds=20),
            "data": {
                "strategy_id": "strat_pnl",
                "contract": contract.model_dump(),
                "side": OrderSide.SELL,
                "filled_quantity": 4,
                "fill_price": 2.30,
            },
        },
    ]

    normalized = normalize_events(events)
    pnl = compute_realized_pnl(normalized)

    assert pnl["strat_pnl"]["SPY"] == pytest.approx(4.20)
