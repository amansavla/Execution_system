"""SQLite persistence for position-to-strategy attribution.

Why this exists: PositionManager is in-memory only. After a process
restart, broker positions are re-seeded but their originating strategy
was previously *guessed* by underlying symbol — which applied the wrong
strategy's exit rules (bit us in live paper trading on 2026-06-10).
This store persists position_id -> strategy_id (+ contract identity) so
startup seeding can recover the true owner and its exit rules.

Table: position_attribution (created in the same SQLite DB as EventStore).
Uses short-lived synchronous sqlite3 connections: writes happen only on
fills/seeding (rare), and WAL mode keeps readers non-blocking.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from src.core.models import Position

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_attribution (
    position_id      TEXT PRIMARY KEY,
    strategy_id      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    expiry           TEXT NOT NULL,
    strike           REAL NOT NULL,
    right            TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    avg_entry_price  REAL NOT NULL,
    entry_time       TEXT,
    status           TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    realized_pnl     REAL,
    close_reason     TEXT,
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_attribution_status
    ON position_attribution (status);
"""


class PositionStore:
    """Persists position->strategy attribution across process restarts."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # :memory: needs a persistent connection or the table vanishes
        self._mem_conn: Optional[sqlite3.Connection] = (
            sqlite3.connect(":memory:") if db_path == ":memory:" else None
        )
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            # Forward migration for DBs created before these columns existed
            for col, typ in (("realized_pnl", "REAL"), ("close_reason", "TEXT"),
                             ("closed_at", "TEXT")):
                try:
                    conn.execute(f"ALTER TABLE position_attribution ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass  # already exists
            conn.commit()
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
    # Write
    # ------------------------------------------------------------------

    def upsert_position(self, pos: Position, close_reason: Optional[str] = None) -> None:
        """Insert or update a position's attribution row.

        Retries on 'database is locked': this write carries realized PnL
        and close reason — silently dropping it left the dashboard's
        history empty (2026-06-11). Callers run this off the event loop
        (asyncio.to_thread), so short blocking retries are safe.
        """
        import time as _time
        for attempt in range(3):
            try:
                self._upsert_position_once(pos, close_reason)
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 2:
                    _time.sleep(0.25 * (attempt + 1))
                    continue
                logger.error("PositionStore upsert failed for %s: %s", pos.position_id, e)
                return
            except Exception as e:
                logger.error("PositionStore upsert failed for %s: %s", pos.position_id, e)
                return

    def _upsert_position_once(self, pos: Position, close_reason: Optional[str] = None) -> None:
        right = pos.contract.right.value if hasattr(pos.contract.right, "value") else str(pos.contract.right)
        side = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
        status = pos.status.value if hasattr(pos.status, "value") else str(pos.status)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO position_attribution
                    (position_id, strategy_id, symbol, expiry, strike, right,
                     side, quantity, avg_entry_price, entry_time, status, updated_at,
                     realized_pnl, close_reason, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    strategy_id=excluded.strategy_id,
                    quantity=excluded.quantity,
                    avg_entry_price=excluded.avg_entry_price,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    realized_pnl=excluded.realized_pnl,
                    close_reason=COALESCE(excluded.close_reason, position_attribution.close_reason),
                    closed_at=COALESCE(excluded.closed_at, position_attribution.closed_at)
                """,
                (
                    str(pos.position_id),
                    pos.strategy_id,
                    pos.contract.symbol,
                    pos.contract.expiry,
                    float(pos.contract.strike),
                    right,
                    side,
                    int(pos.quantity),
                    float(pos.average_entry_price),
                    (pos.entry_time or pos.created_at).isoformat() if (pos.entry_time or pos.created_at) else None,
                    status,
                    datetime.now(UTC).isoformat(),
                    float(pos.realized_pnl) if pos.realized_pnl is not None else None,
                    close_reason,
                    datetime.now(UTC).isoformat() if status.upper() == "CLOSED" else None,
                ),
            )
            conn.commit()
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def find_open_attribution(
        self, symbol: str, expiry: str, strike: float, right: str
    ) -> Optional[dict]:
        """Find the most recent OPEN/OPENING row matching a contract identity.

        Returns dict with strategy_id, position_id, entry_time, avg_entry_price
        or None.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT position_id, strategy_id, entry_time, avg_entry_price, side, quantity
                FROM position_attribution
                WHERE symbol = ? AND expiry = ? AND strike = ? AND right = ?
                  AND status IN ('OPEN', 'OPENING', 'open', 'opening')
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (symbol, expiry, float(strike), right),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "position_id": row[0],
                "strategy_id": row[1],
                "entry_time": row[2],
                "avg_entry_price": row[3],
                "side": row[4],
                "quantity": row[5],
            }
        except Exception as e:
            logger.error("PositionStore lookup failed: %s", e)
            return None
        finally:
            self._close(conn)

    def strategies_traded_on(self, ny_date_iso: str) -> set[str]:
        """Strategy IDs with ANY position whose entry_time falls on the given
        America/New_York calendar date (any status).

        Used by the runner to enforce one-trade-per-day across restarts —
        the in-memory _traded_today sets in strategy providers are lost on
        restart, which let strategies re-enter mid-session (2026-06-11).
        """
        from zoneinfo import ZoneInfo
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT DISTINCT strategy_id, entry_time FROM position_attribution "
                "WHERE entry_time IS NOT NULL"
            )
            out: set[str] = set()
            tz = ZoneInfo("America/New_York")
            for sid, entry_iso in cur.fetchall():
                try:
                    dt = datetime.fromisoformat(entry_iso)
                    if dt.astimezone(tz).date().isoformat() == ny_date_iso:
                        out.add(sid)
                except (ValueError, TypeError):
                    continue
            return out
        except Exception as e:
            logger.error("strategies_traded_on failed: %s", e)
            return set()
        finally:
            self._close(conn)

    def mark_closed_if_absent(self, open_contract_keys: set[tuple]) -> int:
        """Mark OPEN rows closed when their contract is not in the given set.

        Called after startup seeding so rows for positions the broker no
        longer holds don't shadow future lookups. Returns rows updated.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT position_id, symbol, expiry, strike, right FROM position_attribution "
                "WHERE status IN ('OPEN', 'OPENING', 'open', 'opening')"
            )
            stale = [
                r[0] for r in cur.fetchall()
                if (r[1], r[2], float(r[3]), r[4]) not in open_contract_keys
            ]
            for pid in stale:
                conn.execute(
                    "UPDATE position_attribution SET status='CLOSED', updated_at=? WHERE position_id=?",
                    (datetime.now(UTC).isoformat(), pid),
                )
            conn.commit()
            return len(stale)
        except Exception as e:
            logger.error("PositionStore stale-sweep failed: %s", e)
            return 0
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # Dashboard / history queries
    # ------------------------------------------------------------------

    def daily_pnl_summary(self, days: int = 30) -> dict:
        """Realized PnL grouped by America/New_York close date.

        Returns {"today": {"date", "realized_pnl", "closed_positions",
                           "per_strategy": {sid: pnl}},
                 "days": [{"date", "realized_pnl", "closed_positions"}, ...]}
        newest day first, at most `days` entries.
        """
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        today_iso = datetime.now(tz).date().isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT strategy_id, COALESCE(realized_pnl, 0), closed_at "
                "FROM position_attribution "
                "WHERE status IN ('CLOSED', 'closed') AND closed_at IS NOT NULL"
            )
            by_day: dict[str, dict] = {}
            today_by_strategy: dict[str, float] = {}
            for sid, pnl, closed_iso in cur.fetchall():
                try:
                    day = datetime.fromisoformat(closed_iso).astimezone(tz).date().isoformat()
                except (ValueError, TypeError):
                    continue
                d = by_day.setdefault(day, {"date": day, "realized_pnl": 0.0,
                                            "closed_positions": 0})
                d["realized_pnl"] += pnl
                d["closed_positions"] += 1
                if day == today_iso:
                    today_by_strategy[sid] = round(today_by_strategy.get(sid, 0.0) + pnl, 2)
            for d in by_day.values():
                d["realized_pnl"] = round(d["realized_pnl"], 2)
            ordered = sorted(by_day.values(), key=lambda x: x["date"], reverse=True)[:days]
            today = by_day.get(today_iso, {"date": today_iso, "realized_pnl": 0.0,
                                           "closed_positions": 0})
            today["per_strategy"] = today_by_strategy
            return {"today": today, "days": ordered}
        except Exception as e:
            logger.error("daily_pnl_summary failed: %s", e)
            return {"today": {"date": today_iso, "realized_pnl": 0.0,
                              "closed_positions": 0, "per_strategy": {}}, "days": []}
        finally:
            self._close(conn)

    def strategy_pnl_summary(self) -> dict:
        """Realized PnL per strategy across ALL history (survives restarts)."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT strategy_id, COUNT(*), SUM(COALESCE(realized_pnl, 0)) "
                "FROM position_attribution WHERE status IN ('CLOSED', 'closed') "
                "GROUP BY strategy_id"
            )
            return {
                row[0]: {"closed_positions": row[1],
                         "realized_pnl": round(row[2] or 0.0, 2)}
                for row in cur.fetchall()
            }
        except Exception as e:
            logger.error("strategy_pnl_summary failed: %s", e)
            return {}
        finally:
            self._close(conn)

    def closed_positions(self, limit: int = 100) -> list[dict]:
        """Most recent closed positions with realized PnL and close reason."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT strategy_id, symbol, expiry, strike, right, side, quantity, "
                "avg_entry_price, realized_pnl, close_reason, entry_time, closed_at "
                "FROM position_attribution WHERE status IN ('CLOSED', 'closed') "
                "ORDER BY COALESCE(closed_at, updated_at) DESC LIMIT ?",
                (limit,),
            )
            cols = ["strategy_id", "symbol", "expiry", "strike", "right", "side",
                    "quantity", "avg_entry_price", "realized_pnl", "close_reason",
                    "entry_time", "closed_at"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error("closed_positions failed: %s", e)
            return []
        finally:
            self._close(conn)
