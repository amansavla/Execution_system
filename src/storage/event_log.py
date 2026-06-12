"""EventStore — high-level audit log interface.

Replaces EventStoreStub from Phase 4/5. Provides:
- Synchronous log_callback() that enqueues writes (non-blocking)
- Async background writer that drains the queue to SQLite
- Typed query pass-through to EventRepository
- Graceful shutdown with flush

Event types logged (per AGENTS.md rule 10):
  signal, risk_decision, order_event, fill_event, position_update,
  exit_decision, manual_override, reconciliation_event, error
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel

from src.storage.db import create_connection, initialize_schema
from src.storage.repositories import EventRecord, EventRepository

logger = logging.getLogger(__name__)

# Valid event types per AGENTS.md rule 10
VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "signal",
        "risk_decision",
        "order_event",
        "fill_event",
        "position_update",
        "exit_decision",
        "manual_override",
        "reconciliation_event",
        "error",
        # Internal lifecycle events logged by OrderManager / MockBroker
        "order_callback",
        "fill_callback",
        "order_state_transition",
        "fill_received",
        "position_opened",
        "position_added",
        "position_partial_exit",
        "position_closed",
        "position_forced_closed",
        "exit_triggered",
    }
)


class EventStore:
    """Async-capable event store backed by SQLite.

    Designed as a drop-in replacement for EventStoreStub.
    The synchronous log_callback() method puts events onto an asyncio
    queue. A background task drains the queue and writes to SQLite.

    Usage:
        store = EventStore()
        await store.start()          # opens DB, starts writer
        store.log_callback("signal", signal_model)
        records = await store.query_by_type("signal")
        await store.stop()           # flush + close
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._repo: Optional[EventRepository] = None
        self._queue: asyncio.Queue[tuple[str, str, str, Optional[str], dict]] = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._running = False

        # In-memory event list for backward compatibility with
        # tests that inspect .events directly (like EventStoreStub)
        self.events: list[dict] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open database, create schema, start background writer."""
        self._conn = await create_connection(self._db_path)
        await initialize_schema(self._conn)
        self._repo = EventRepository(self._conn)
        self._running = True
        self._writer_task = asyncio.create_task(self._drain_queue())

    async def stop(self) -> None:
        """Flush pending writes and close database connection."""
        self._running = False
        if self._writer_task and not self._writer_task.done():
            # Signal writer to stop by putting a sentinel
            await self._queue.put(None)  # type: ignore[arg-type]
            await self._writer_task
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._repo = None

    # ------------------------------------------------------------------
    # Write interface (synchronous — non-blocking enqueue)
    # ------------------------------------------------------------------

    def log_callback(self, callback_type: str, data: BaseModel | dict) -> None:
        """Record an event. Drop-in replacement for EventStoreStub.log_callback.

        This is intentionally synchronous so callers (broker callbacks,
        order manager helpers) don't need to await. The actual DB write
        happens asynchronously on the background writer task.

        Args:
            callback_type: The event type string.
            data: Pydantic model or dict payload.
        """
        now = datetime.now(UTC)
        event_id = uuid4().hex

        # Serialize payload
        if hasattr(data, "model_dump"):
            payload = data.model_dump(mode="json")
        elif isinstance(data, dict):
            payload = data
        else:
            payload = {"raw": str(data)}

        # Extract strategy_id from payload if present
        strategy_id = None
        if isinstance(payload, dict):
            strategy_id = payload.get("strategy_id")

        timestamp_iso = now.isoformat()

        # In-memory list for backward compat / test inspection.
        # Trimmed to bound memory over a full trading day (the runner logs
        # tens of thousands of events per session).
        self.events.append(
            {
                "type": callback_type,
                "data": payload,
                "timestamp": now,
            }
        )
        if len(self.events) > 20000:
            del self.events[:10000]

        # Enqueue for async write (best-effort if writer not started)
        try:
            self._queue.put_nowait(
                (event_id, callback_type, timestamp_iso, strategy_id, payload)
            )
        except Exception:
            logger.warning(
                "EventStore queue put failed for event_type=%s", callback_type
            )

    # ------------------------------------------------------------------
    # Query interface (async)
    # ------------------------------------------------------------------

    async def query_by_type(self, event_type: str) -> list[EventRecord]:
        """Query persisted events by type."""
        if not self._repo:
            return []
        return await self._repo.get_by_type(event_type)

    async def query_by_strategy(self, strategy_id: str) -> list[EventRecord]:
        """Query persisted events by strategy_id."""
        if not self._repo:
            return []
        return await self._repo.get_by_strategy(strategy_id)

    async def query_by_time_range(
        self, start: datetime, end: datetime
    ) -> list[EventRecord]:
        """Query persisted events within a time range."""
        if not self._repo:
            return []
        return await self._repo.get_by_time_range(start, end)

    async def query_by_type_and_strategy(
        self, event_type: str, strategy_id: str
    ) -> list[EventRecord]:
        """Query events by both type and strategy."""
        if not self._repo:
            return []
        return await self._repo.get_by_type_and_strategy(event_type, strategy_id)

    async def query_all(self) -> list[EventRecord]:
        """Return all persisted events."""
        if not self._repo:
            return []
        return await self._repo.get_all()

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    async def _drain_queue(self) -> None:
        """Background task: pull items from queue and write to SQLite.

        Handles CancelledError gracefully: if the task is cancelled
        (e.g. process exit without stop()), it drains remaining queue
        items before exiting so events are not silently lost.
        """
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    # Sentinel received — drain remaining then exit
                    await self._drain_remaining()
                    break

                # Batch whatever else is already queued (cap 100) into ONE
                # transaction. Per-event commits held the write lock almost
                # continuously during bursts, starving position_store /
                # runtime_state writers ('database is locked', 2026-06-11).
                batch = [item]
                while len(batch) < 100:
                    try:
                        nxt = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:
                        await self._write_batch(batch)
                        await self._drain_remaining()
                        return
                    batch.append(nxt)
                await self._write_batch(batch)
        except asyncio.CancelledError:
            # Task was cancelled without stop() — drain what we can
            logger.info("EventStore writer cancelled, draining remaining events")
            await self._drain_remaining()
            raise  # re-raise per asyncio convention

    async def _write_batch(self, batch: list) -> None:
        """Insert a batch of queued events in a single transaction."""
        if not self._repo:
            return
        try:
            await self._repo.insert_many(batch)
        except Exception:
            logger.exception("EventStore batch write failed (%d events)", len(batch))

    async def _drain_remaining(self) -> None:
        """Drain all remaining items from the queue to SQLite."""
        while not self._queue.empty():
            try:
                remaining = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if remaining is not None and self._repo:
                event_id, event_type, ts, sid, payload = remaining
                try:
                    await self._repo.insert(
                        event_id, event_type, ts, sid, payload
                    )
                except Exception:
                    logger.exception("EventStore flush write failed")

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def flush(self) -> None:
        """Wait until the write queue is fully drained.

        Useful in tests to ensure all log_callback writes have persisted
        before querying.
        """
        # Spin until the queue is empty and the writer has caught up
        while not self._queue.empty():
            await asyncio.sleep(0.01)
        # One more sleep to let the last item finish writing
        await asyncio.sleep(0.01)

