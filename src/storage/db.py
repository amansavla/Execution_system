"""Database schema, connection management, and migrations for EventStore.

All raw SQL lives here. Other modules use repositories.py for typed access.
Uses aiosqlite for async SQLite operations.
"""

from __future__ import annotations

import aiosqlite

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    strategy_id  TEXT,
    payload      TEXT NOT NULL
);
"""

_CREATE_INDEX_EVENT_TYPE = """
CREATE INDEX IF NOT EXISTS idx_events_event_type
ON events (event_type);
"""

_CREATE_INDEX_STRATEGY_ID = """
CREATE INDEX IF NOT EXISTS idx_events_strategy_id
ON events (strategy_id);
"""

_CREATE_INDEX_TIMESTAMP = """
CREATE INDEX IF NOT EXISTS idx_events_timestamp
ON events (timestamp);
"""

# All DDL statements in migration order
_MIGRATIONS: list[str] = [
    _CREATE_EVENTS_TABLE,
    _CREATE_INDEX_EVENT_TYPE,
    _CREATE_INDEX_STRATEGY_ID,
    _CREATE_INDEX_TIMESTAMP,
]

# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

INSERT_EVENT = """
INSERT INTO events (event_id, event_type, timestamp, strategy_id, payload)
VALUES (?, ?, ?, ?, ?);
"""

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

SELECT_EVENTS_BY_TYPE = """
SELECT event_id, event_type, timestamp, strategy_id, payload
FROM events
WHERE event_type = ?
ORDER BY timestamp ASC;
"""

SELECT_EVENTS_BY_STRATEGY = """
SELECT event_id, event_type, timestamp, strategy_id, payload
FROM events
WHERE strategy_id = ?
ORDER BY timestamp ASC;
"""

SELECT_EVENTS_BY_TIME_RANGE = """
SELECT event_id, event_type, timestamp, strategy_id, payload
FROM events
WHERE timestamp >= ? AND timestamp <= ?
ORDER BY timestamp ASC;
"""

SELECT_EVENTS_BY_TYPE_AND_STRATEGY = """
SELECT event_id, event_type, timestamp, strategy_id, payload
FROM events
WHERE event_type = ? AND strategy_id = ?
ORDER BY timestamp ASC;
"""

SELECT_ALL_EVENTS = """
SELECT event_id, event_type, timestamp, strategy_id, payload
FROM events
ORDER BY timestamp ASC;
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

async def create_connection(db_path: str = ":memory:") -> aiosqlite.Connection:
    """Open a connection and return it. Caller owns the lifecycle.

    Args:
        db_path: Path to SQLite file, or ":memory:" for in-memory.

    Returns:
        An open aiosqlite.Connection with WAL mode enabled and a 30s timeout.
    """
    conn = await aiosqlite.connect(db_path, timeout=30.0)
    # WAL mode for better concurrent read/write performance
    await conn.execute("PRAGMA journal_mode=WAL;")
    return conn


async def initialize_schema(conn: aiosqlite.Connection) -> None:
    """Run all schema migrations on the given connection.

    Safe to call repeatedly — all statements use IF NOT EXISTS.
    """
    try:
        for ddl in _MIGRATIONS:
            await conn.execute(ddl)
        await conn.commit()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        raise

