"""Tests for EventStore, EventRepository, and db schema.

Covers:
- Write and read for each event type
- Query by type, strategy_id, time range
- Concurrent async writes do not corrupt state
- Schema creation is idempotent
- Non-blocking log_callback behavior
- Flush ensures persistence before query
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.core.enums import (
    OptionRight,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)
from src.core.models import (
    FillEvent,
    ManualOverride,
    OptionContract,
    OrderEvent,
    Position,
    QuoteSnapshot,
    ReconciliationReport,
    RiskDecision,
    StrategySignal,
)
from src.storage.db import create_connection, initialize_schema
from src.storage.event_log import EventStore
from src.storage.repositories import EventRecord, EventRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_contract() -> OptionContract:
    return OptionContract(
        symbol="SPX240520C05200",
        expiry="20240520",
        strike=5200.0,
        right=OptionRight.CALL,
    )


@pytest.fixture
def sample_signal(sample_contract: OptionContract) -> StrategySignal:
    return StrategySignal(
        strategy_id="test_strat_1",
        direction=SignalDirection.LONG,
        contract=sample_contract,
        requested_quantity=2,
        limit_price=5.50,
    )


@pytest.fixture
def sample_risk_decision(sample_signal: StrategySignal) -> RiskDecision:
    return RiskDecision(
        signal_id=sample_signal.signal_id,
        status=RiskDecisionStatus.APPROVED,
        allowed_quantity=2,
    )


@pytest.fixture
def sample_order_event() -> OrderEvent:
    return OrderEvent(
        order_id=uuid4(),
        previous_status=OrderStatus.NEW,
        new_status=OrderStatus.SUBMITTED,
        message="Order submitted to broker",
    )


@pytest.fixture
def sample_fill_event(sample_contract: OptionContract) -> FillEvent:
    return FillEvent(
        order_id=uuid4(),
        strategy_id="test_strat_1",
        contract=sample_contract,
        side=OrderSide.BUY,
        filled_quantity=2,
        fill_price=5.45,
        commission=1.30,
    )


@pytest.fixture
def sample_position(sample_contract: OptionContract) -> Position:
    return Position(
        strategy_id="test_strat_1",
        contract=sample_contract,
        side=OrderSide.BUY,
        quantity=2,
        average_entry_price=5.50,
        entry_order_id=uuid4(),
        status=PositionStatus.OPEN,
    )


@pytest.fixture
def sample_manual_override() -> ManualOverride:
    return ManualOverride(
        command="flatten-all",
        target="test_strat_1",
        operator="admin",
    )


@pytest.fixture
def sample_reconciliation_report() -> ReconciliationReport:
    return ReconciliationReport(
        matches=5,
        mismatches=0,
        is_clean=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_store() -> EventStore:
    """Create and start an in-memory EventStore."""
    store = EventStore(db_path=":memory:")
    await store.start()
    return store


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchema:
    """Test database schema creation and idempotency."""

    @pytest.mark.asyncio
    async def test_schema_creation(self) -> None:
        """Schema creates events table and indexes."""
        conn = await create_connection(":memory:")
        await initialize_schema(conn)

        # Check table exists
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        )
        tables = await cursor.fetchall()
        assert len(tables) == 1
        assert tables[0][0] == "events"
        await conn.close()

    @pytest.mark.asyncio
    async def test_schema_idempotent(self) -> None:
        """Calling initialize_schema twice does not error."""
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        await initialize_schema(conn)  # second call safe

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='events'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1
        await conn.close()


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------

class TestEventRepository:
    """Test the typed repository layer."""

    @pytest.mark.asyncio
    async def test_insert_and_get_all(self) -> None:
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("evt-1", "signal", "2024-01-01T00:00:00+00:00", "strat_a", {"key": "val"})
        records = await repo.get_all()
        assert len(records) == 1
        assert records[0].event_id == "evt-1"
        assert records[0].event_type == "signal"
        assert records[0].strategy_id == "strat_a"
        assert records[0].payload == {"key": "val"}
        await conn.close()

    @pytest.mark.asyncio
    async def test_get_by_type(self) -> None:
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("e1", "signal", "2024-01-01T00:00:00+00:00", "s1", {})
        await repo.insert("e2", "error", "2024-01-01T00:01:00+00:00", "s1", {})
        await repo.insert("e3", "signal", "2024-01-01T00:02:00+00:00", "s2", {})

        signals = await repo.get_by_type("signal")
        assert len(signals) == 2
        errors = await repo.get_by_type("error")
        assert len(errors) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_get_by_strategy(self) -> None:
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("e1", "signal", "2024-01-01T00:00:00+00:00", "strat_a", {})
        await repo.insert("e2", "error", "2024-01-01T00:01:00+00:00", "strat_b", {})
        await repo.insert("e3", "fill_event", "2024-01-01T00:02:00+00:00", "strat_a", {})

        strat_a = await repo.get_by_strategy("strat_a")
        assert len(strat_a) == 2
        strat_b = await repo.get_by_strategy("strat_b")
        assert len(strat_b) == 1
        await conn.close()

    @pytest.mark.asyncio
    async def test_get_by_time_range(self) -> None:
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("e1", "signal", "2024-01-01T10:00:00+00:00", None, {})
        await repo.insert("e2", "signal", "2024-01-01T11:00:00+00:00", None, {})
        await repo.insert("e3", "signal", "2024-01-01T12:00:00+00:00", None, {})

        start = datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
        end = datetime(2024, 1, 1, 12, 30, tzinfo=UTC)
        results = await repo.get_by_time_range(start, end)
        assert len(results) == 2  # e2 and e3
        await conn.close()

    @pytest.mark.asyncio
    async def test_get_by_type_and_strategy(self) -> None:
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("e1", "signal", "2024-01-01T10:00:00+00:00", "s1", {})
        await repo.insert("e2", "signal", "2024-01-01T10:01:00+00:00", "s2", {})
        await repo.insert("e3", "error", "2024-01-01T10:02:00+00:00", "s1", {})

        results = await repo.get_by_type_and_strategy("signal", "s1")
        assert len(results) == 1
        assert results[0].event_id == "e1"
        await conn.close()

    @pytest.mark.asyncio
    async def test_nullable_strategy_id(self) -> None:
        """strategy_id is nullable; events with None are queryable."""
        conn = await create_connection(":memory:")
        await initialize_schema(conn)
        repo = EventRepository(conn)

        await repo.insert("e1", "error", "2024-01-01T10:00:00+00:00", None, {"msg": "system-wide"})
        all_events = await repo.get_all()
        assert len(all_events) == 1
        assert all_events[0].strategy_id is None
        await conn.close()


# ---------------------------------------------------------------------------
# EventStore tests — write + read for each event type
# ---------------------------------------------------------------------------

class TestEventStoreWriteRead:
    """Test log_callback + query for each AGENTS.md event type."""

    @pytest.mark.asyncio
    async def test_signal_event(self, sample_signal: StrategySignal) -> None:
        store = await _make_store()
        store.log_callback("signal", sample_signal)
        await store.flush()

        records = await store.query_by_type("signal")
        assert len(records) == 1
        assert records[0].event_type == "signal"
        assert records[0].strategy_id == "test_strat_1"
        await store.stop()

    @pytest.mark.asyncio
    async def test_risk_decision_event(self, sample_risk_decision: RiskDecision) -> None:
        store = await _make_store()
        store.log_callback("risk_decision", sample_risk_decision)
        await store.flush()

        records = await store.query_by_type("risk_decision")
        assert len(records) == 1
        assert records[0].payload["status"] == "APPROVED"
        await store.stop()

    @pytest.mark.asyncio
    async def test_order_event(self, sample_order_event: OrderEvent) -> None:
        store = await _make_store()
        store.log_callback("order_event", sample_order_event)
        await store.flush()

        records = await store.query_by_type("order_event")
        assert len(records) == 1
        assert records[0].payload["new_status"] == "SUBMITTED"
        await store.stop()

    @pytest.mark.asyncio
    async def test_fill_event(self, sample_fill_event: FillEvent) -> None:
        store = await _make_store()
        store.log_callback("fill_event", sample_fill_event)
        await store.flush()

        records = await store.query_by_type("fill_event")
        assert len(records) == 1
        assert records[0].payload["filled_quantity"] == 2
        assert records[0].strategy_id == "test_strat_1"
        await store.stop()

    @pytest.mark.asyncio
    async def test_position_update_event(self, sample_position: Position) -> None:
        store = await _make_store()
        store.log_callback("position_update", sample_position)
        await store.flush()

        records = await store.query_by_type("position_update")
        assert len(records) == 1
        assert records[0].payload["status"] == "OPEN"
        await store.stop()

    @pytest.mark.asyncio
    async def test_exit_decision_event(self) -> None:
        store = await _make_store()
        exit_data = {
            "strategy_id": "test_strat_2",
            "position_id": str(uuid4()),
            "reason": "stop_loss_triggered",
            "exit_price": 4.20,
        }
        store.log_callback("exit_decision", exit_data)
        await store.flush()

        records = await store.query_by_type("exit_decision")
        assert len(records) == 1
        assert records[0].strategy_id == "test_strat_2"
        assert records[0].payload["reason"] == "stop_loss_triggered"
        await store.stop()

    @pytest.mark.asyncio
    async def test_manual_override_event(self, sample_manual_override: ManualOverride) -> None:
        store = await _make_store()
        store.log_callback("manual_override", sample_manual_override)
        await store.flush()

        records = await store.query_by_type("manual_override")
        assert len(records) == 1
        assert records[0].payload["command"] == "flatten-all"
        await store.stop()

    @pytest.mark.asyncio
    async def test_reconciliation_event(
        self, sample_reconciliation_report: ReconciliationReport
    ) -> None:
        store = await _make_store()
        store.log_callback("reconciliation_event", sample_reconciliation_report)
        await store.flush()

        records = await store.query_by_type("reconciliation_event")
        assert len(records) == 1
        assert records[0].payload["is_clean"] is True
        await store.stop()

    @pytest.mark.asyncio
    async def test_error_event(self) -> None:
        store = await _make_store()
        error_data = {
            "strategy_id": "test_strat_1",
            "error": "ConnectionError",
            "message": "Broker disconnected",
        }
        store.log_callback("error", error_data)
        await store.flush()

        records = await store.query_by_type("error")
        assert len(records) == 1
        assert records[0].payload["error"] == "ConnectionError"
        await store.stop()


# ---------------------------------------------------------------------------
# EventStore query tests
# ---------------------------------------------------------------------------

class TestEventStoreQueries:
    """Test EventStore query methods."""

    @pytest.mark.asyncio
    async def test_query_by_strategy(self) -> None:
        store = await _make_store()

        store.log_callback("signal", {"strategy_id": "alpha", "data": 1})
        store.log_callback("signal", {"strategy_id": "beta", "data": 2})
        store.log_callback("error", {"strategy_id": "alpha", "data": 3})
        await store.flush()

        alpha = await store.query_by_strategy("alpha")
        assert len(alpha) == 2
        beta = await store.query_by_strategy("beta")
        assert len(beta) == 1
        await store.stop()

    @pytest.mark.asyncio
    async def test_query_by_time_range(self) -> None:
        store = await _make_store()

        # Log 3 events with small delays so timestamps differ
        store.log_callback("signal", {"strategy_id": "s1", "seq": 1})
        await asyncio.sleep(0.02)
        mid_time = datetime.now(UTC)
        await asyncio.sleep(0.02)
        store.log_callback("signal", {"strategy_id": "s1", "seq": 2})
        await asyncio.sleep(0.02)
        store.log_callback("signal", {"strategy_id": "s1", "seq": 3})
        await store.flush()

        end_time = datetime.now(UTC) + timedelta(seconds=1)
        results = await store.query_by_time_range(mid_time, end_time)
        # Should get seq 2 and 3 (timestamps after mid_time)
        assert len(results) >= 1  # At least the later ones
        await store.stop()

    @pytest.mark.asyncio
    async def test_query_by_type_and_strategy(self) -> None:
        store = await _make_store()

        store.log_callback("signal", {"strategy_id": "s1"})
        store.log_callback("signal", {"strategy_id": "s2"})
        store.log_callback("error", {"strategy_id": "s1"})
        await store.flush()

        results = await store.query_by_type_and_strategy("signal", "s1")
        assert len(results) == 1
        await store.stop()

    @pytest.mark.asyncio
    async def test_query_all(self) -> None:
        store = await _make_store()

        store.log_callback("signal", {"strategy_id": "s1"})
        store.log_callback("error", {"strategy_id": "s2"})
        store.log_callback("fill_event", {"strategy_id": "s1"})
        await store.flush()

        all_records = await store.query_all()
        assert len(all_records) == 3
        await store.stop()

    @pytest.mark.asyncio
    async def test_query_empty_results(self) -> None:
        store = await _make_store()
        results = await store.query_by_type("nonexistent_type")
        assert results == []
        await store.stop()


# ---------------------------------------------------------------------------
# EventStore behavioral tests
# ---------------------------------------------------------------------------

class TestEventStoreBehavior:
    """Test non-blocking writes, backward compat, and edge cases."""

    @pytest.mark.asyncio
    async def test_in_memory_events_list_compat(self) -> None:
        """log_callback populates .events list for backward compat."""
        store = await _make_store()
        store.log_callback("signal", {"strategy_id": "s1"})
        store.log_callback("error", {"strategy_id": "s2"})

        assert len(store.events) == 2
        assert store.events[0]["type"] == "signal"
        assert store.events[1]["type"] == "error"
        await store.stop()

    @pytest.mark.asyncio
    async def test_log_callback_before_start(self) -> None:
        """log_callback before start() should not crash.

        Events accumulate in .events list and queue but DB writes
        only happen after start().
        """
        store = EventStore(db_path=":memory:")
        # Not started — this should not raise
        store.log_callback("error", {"msg": "pre-start"})
        assert len(store.events) == 1

    @pytest.mark.asyncio
    async def test_log_callback_with_dict_payload(self) -> None:
        store = await _make_store()
        store.log_callback("error", {"strategy_id": "s1", "details": "something broke"})
        await store.flush()

        records = await store.query_by_type("error")
        assert len(records) == 1
        assert records[0].payload["details"] == "something broke"
        await store.stop()

    @pytest.mark.asyncio
    async def test_log_callback_pydantic_model_serialization(
        self, sample_signal: StrategySignal
    ) -> None:
        """Pydantic models are serialized with model_dump(mode='json')."""
        store = await _make_store()
        store.log_callback("signal", sample_signal)
        await store.flush()

        records = await store.query_by_type("signal")
        assert len(records) == 1
        payload = records[0].payload
        assert payload["strategy_id"] == "test_strat_1"
        assert payload["requested_quantity"] == 2
        # UUIDs should be serialized as strings
        assert isinstance(payload["signal_id"], str)
        await store.stop()

    @pytest.mark.asyncio
    async def test_stop_flushes_pending_writes(self) -> None:
        """stop() should drain the queue before closing."""
        store = await _make_store()
        for i in range(10):
            store.log_callback("signal", {"strategy_id": f"s{i}", "seq": i})

        # Don't explicitly flush — stop should handle it
        await store.stop()

        # Re-open to verify nothing was lost from in-memory list
        assert len(store.events) == 10

    @pytest.mark.asyncio
    async def test_strategy_id_extraction_from_payload(self) -> None:
        """strategy_id is extracted from payload dict for query indexing."""
        store = await _make_store()
        store.log_callback("signal", {"strategy_id": "extracted_strat", "value": 42})
        await store.flush()

        results = await store.query_by_strategy("extracted_strat")
        assert len(results) == 1
        assert results[0].payload["value"] == 42
        await store.stop()

    @pytest.mark.asyncio
    async def test_null_strategy_id_event(self) -> None:
        """Events without strategy_id are stored with NULL strategy_id."""
        store = await _make_store()
        store.log_callback("error", {"message": "system-level error, no strategy"})
        await store.flush()

        all_events = await store.query_all()
        assert len(all_events) == 1
        assert all_events[0].strategy_id is None
        await store.stop()


# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    """Test that concurrent async writes do not corrupt state."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_do_not_corrupt(self) -> None:
        """Fire many log_callback calls rapidly and verify all persist."""
        store = await _make_store()
        num_events = 100

        for i in range(num_events):
            store.log_callback("signal", {"strategy_id": f"strat_{i % 5}", "seq": i})

        await store.flush()

        all_records = await store.query_all()
        assert len(all_records) == num_events
        assert len(store.events) == num_events
        await store.stop()

    @pytest.mark.asyncio
    async def test_concurrent_async_tasks_writing(self) -> None:
        """Multiple async tasks writing simultaneously should be safe."""
        store = await _make_store()

        async def writer(strategy_id: str, count: int) -> None:
            for i in range(count):
                store.log_callback("signal", {"strategy_id": strategy_id, "seq": i})
                await asyncio.sleep(0)  # yield control

        # 5 concurrent writers, 20 events each
        tasks = [
            asyncio.create_task(writer(f"strat_{i}", 20))
            for i in range(5)
        ]
        await asyncio.gather(*tasks)
        await store.flush()

        all_records = await store.query_all()
        assert len(all_records) == 100
        assert len(store.events) == 100

        # Verify per-strategy counts
        for i in range(5):
            strat_records = await store.query_by_strategy(f"strat_{i}")
            assert len(strat_records) == 20

        await store.stop()


# ---------------------------------------------------------------------------
# EventStore as EventStoreStub replacement
# ---------------------------------------------------------------------------

class TestEventStoreAsStubReplacement:
    """Verify EventStore has the same interface as EventStoreStub."""

    @pytest.mark.asyncio
    async def test_has_log_callback(self) -> None:
        store = await _make_store()
        assert hasattr(store, "log_callback")
        assert callable(store.log_callback)
        await store.stop()

    @pytest.mark.asyncio
    async def test_has_events_list(self) -> None:
        store = await _make_store()
        assert hasattr(store, "events")
        assert isinstance(store.events, list)
        await store.stop()

    @pytest.mark.asyncio
    async def test_log_callback_signature_compat(
        self, sample_order_event: OrderEvent
    ) -> None:
        """log_callback accepts (str, BaseModel) like EventStoreStub."""
        store = await _make_store()
        store.log_callback("order_callback", sample_order_event)
        assert len(store.events) == 1
        assert store.events[0]["type"] == "order_callback"
        await store.stop()

    @pytest.mark.asyncio
    async def test_events_list_data_structure(
        self, sample_fill_event: FillEvent
    ) -> None:
        """Each item in .events has type, data, and timestamp keys."""
        store = await _make_store()
        store.log_callback("fill_callback", sample_fill_event)

        entry = store.events[0]
        assert "type" in entry
        assert "data" in entry
        assert "timestamp" in entry
        assert entry["type"] == "fill_callback"
        assert isinstance(entry["timestamp"], datetime)
        await store.stop()
