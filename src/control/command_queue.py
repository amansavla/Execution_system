"""SQLite-backed command queue: the control plane between dashboard and runner.

Design (user-approved): the dashboard process NEVER talks to the broker.
It inserts rows into the `commands` table; the runner polls once per tick
and routes each command through the exact same code path as the automated
equivalent (ExitManager / OrderManager / OverrideManager). No parallel
execution paths exist.

Command types and payloads (payload is JSON):
    exit_position   {"position_id": "<uuid>"}
    cancel_order    {"order_id": "<uuid>"}
    pause_strategy  {"strategy_id": "...", "paused": true|false}
    flatten_all     {}

Lifecycle: pending -> done | failed (result column carries detail).
Commands are durable: an unprocessed command survives a runner restart.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VALID_COMMAND_TYPES = {
    "exit_position", "cancel_order", "pause_strategy", "flatten_all",
    "update_strategy", "unlock_system", "lock_system", "restart_runner",
    "shutdown_runner",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    command_id   TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands (status);
"""


class CommandQueue:
    """Durable SQLite command queue (single-writer-per-side, WAL mode)."""

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
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            pass  # WAL conversion can fail under contention; busy_timeout still applies
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if conn is not self._mem_conn:
            conn.close()

    # ------------------------------------------------------------------
    # Producer side (dashboard)
    # ------------------------------------------------------------------

    def enqueue(self, command_type: str, payload: Optional[dict] = None) -> str:
        """Insert a pending command; returns its command_id."""
        if command_type not in VALID_COMMAND_TYPES:
            raise ValueError(f"Unknown command type: {command_type}")
        command_id = str(uuid.uuid4())
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO commands (command_id, type, payload, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (command_id, command_type, json.dumps(payload or {}),
                 datetime.now(UTC).isoformat()),
            )
            conn.commit()
        finally:
            self._close(conn)
        return command_id

    # ------------------------------------------------------------------
    # Consumer side (runner)
    # ------------------------------------------------------------------

    def fetch_pending(self, limit: int = 10) -> list[dict]:
        """Oldest pending commands, parsed."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT command_id, type, payload, created_at FROM commands "
                "WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            out = []
            for cid, ctype, payload, created in cur.fetchall():
                try:
                    parsed = json.loads(payload or "{}")
                except json.JSONDecodeError:
                    parsed = {}
                out.append({"command_id": cid, "type": ctype,
                            "payload": parsed, "created_at": created})
            return out
        finally:
            self._close(conn)

    def mark(self, command_id: str, status: str, result: str = "") -> None:
        """Mark a command done or failed."""
        if status not in ("done", "failed"):
            raise ValueError(f"Invalid terminal status: {status}")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE commands SET status=?, processed_at=?, result=? WHERE command_id=?",
                (status, datetime.now(UTC).isoformat(), result[:500], command_id),
            )
            conn.commit()
        finally:
            self._close(conn)

    def recent(self, limit: int = 50) -> list[dict]:
        """Most recent commands (any status) for dashboard display."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT command_id, type, payload, status, created_at, processed_at, result "
                "FROM commands ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            cols = ["command_id", "type", "payload", "status", "created_at",
                    "processed_at", "result"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            self._close(conn)
