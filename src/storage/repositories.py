"""Typed repository layer over SQLite event storage.

Provides structured query methods. No raw SQL here — all SQL
is imported from db.py. Returns typed EventRecord objects.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import aiosqlite
from pydantic import BaseModel, Field

from src.storage.db import (
    INSERT_EVENT,
    SELECT_ALL_EVENTS,
    SELECT_EVENTS_BY_STRATEGY,
    SELECT_EVENTS_BY_TIME_RANGE,
    SELECT_EVENTS_BY_TYPE,
    SELECT_EVENTS_BY_TYPE_AND_STRATEGY,
)


# ---------------------------------------------------------------------------
# Event record model
# ---------------------------------------------------------------------------

class EventRecord(BaseModel):
    """Typed representation of a persisted event row."""

    event_id: str
    event_type: str
    timestamp: str  # ISO-8601 string as stored in SQLite
    strategy_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)


def _row_to_record(row: tuple) -> EventRecord:
    """Convert a raw SQLite row tuple to an EventRecord."""
    event_id, event_type, timestamp, strategy_id, payload_json = row
    return EventRecord(
        event_id=event_id,
        event_type=event_type,
        timestamp=timestamp,
        strategy_id=strategy_id,
        payload=json.loads(payload_json) if payload_json else {},
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class EventRepository:
    """Typed query interface over the events table.

    All SQL is sourced from db.py. This class only binds parameters
    and maps results to EventRecord objects.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # -- Write ---------------------------------------------------------------

    async def insert(
        self,
        event_id: str,
        event_type: str,
        timestamp: str,
        strategy_id: Optional[str],
        payload: dict,
    ) -> None:
        """Insert a single event record."""
        payload_json = json.dumps(payload, default=str)
        try:
            await self._conn.execute(
                INSERT_EVENT,
                (event_id, event_type, timestamp, strategy_id, payload_json),
            )
            await self._conn.commit()
        except Exception:
            try:
                await self._conn.rollback()
            except Exception:
                pass
            raise

    async def insert_many(self, items: list[tuple]) -> None:
        """Insert a batch of (event_id, type, timestamp, strategy_id, payload)
        rows in a single transaction — one write-lock acquisition per batch
        instead of per event."""
        rows = [
            (event_id, event_type, ts, sid, json.dumps(payload, default=str))
            for event_id, event_type, ts, sid, payload in items
        ]
        try:
            await self._conn.executemany(INSERT_EVENT, rows)
            await self._conn.commit()
        except Exception:
            try:
                await self._conn.rollback()
            except Exception:
                pass
            raise


    # -- Read ----------------------------------------------------------------

    async def get_by_type(self, event_type: str) -> list[EventRecord]:
        """Query events filtered by event_type."""
        cursor = await self._conn.execute(SELECT_EVENTS_BY_TYPE, (event_type,))
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def get_by_strategy(self, strategy_id: str) -> list[EventRecord]:
        """Query events filtered by strategy_id."""
        cursor = await self._conn.execute(SELECT_EVENTS_BY_STRATEGY, (strategy_id,))
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def get_by_time_range(
        self,
        start: datetime,
        end: datetime,
    ) -> list[EventRecord]:
        """Query events within a UTC time range (inclusive)."""
        start_iso = start.isoformat()
        end_iso = end.isoformat()
        cursor = await self._conn.execute(
            SELECT_EVENTS_BY_TIME_RANGE, (start_iso, end_iso)
        )
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def get_by_type_and_strategy(
        self,
        event_type: str,
        strategy_id: str,
    ) -> list[EventRecord]:
        """Query events filtered by both event_type and strategy_id."""
        cursor = await self._conn.execute(
            SELECT_EVENTS_BY_TYPE_AND_STRATEGY, (event_type, strategy_id)
        )
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]

    async def get_all(self) -> list[EventRecord]:
        """Return all events, ordered by timestamp ascending."""
        cursor = await self._conn.execute(SELECT_ALL_EVENTS)
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]
