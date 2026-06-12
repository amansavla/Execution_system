"""Single-row runtime state snapshot, written by the runner each tick.

This is how the dashboard sees live positions/orders/PnL WITHOUT ever
talking to the broker: the runner (sole broker owner) serializes its view
into the `runtime_state` table; the dashboard process only reads SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class RuntimeStateStore:
    """Writer/reader for the single-row runtime snapshot."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._mem_conn: Optional[sqlite3.Connection] = (
            sqlite3.connect(":memory:") if db_path == ":memory:" else None
        )
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            self._close(conn)

    def _connect(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000;")
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            pass  # WAL conversion can fail under contention; busy_timeout still applies
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if conn is not self._mem_conn:
            conn.close()

    def write(self, state: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO runtime_state (id, state, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (json.dumps(state, default=str), datetime.now(UTC).isoformat()),
            )
            conn.commit()
        except Exception as e:
            logger.error("runtime_state write failed: %s", e)
        finally:
            self._close(conn)

    def read(self) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT state, updated_at FROM runtime_state WHERE id = 1")
            row = cur.fetchone()
            if row is None:
                return None
            state = json.loads(row[0])
            state["_updated_at"] = row[1]
            return state
        except Exception as e:
            logger.error("runtime_state read failed: %s", e)
            return None
        finally:
            self._close(conn)
