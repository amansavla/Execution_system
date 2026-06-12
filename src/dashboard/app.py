"""FastAPI operator dashboard — DB-only control plane.

HARD CONSTRAINT: this process NEVER talks to the broker. It has no broker
imports, no ib_async, no order/position managers. It reads SQLite tables
that the runner writes (runtime_state, events, commands, position_
attribution) and issues commands by inserting rows into the `commands`
queue, which the runner drains through its standard execution paths.

Run:
    python3 -m uvicorn src.dashboard.app:app --port 8500
    DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app

The DB path comes from the DASHBOARD_DB env var (default data/events.db).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.control.command_queue import VALID_COMMAND_TYPES, CommandQueue
from src.storage.runtime_state import RuntimeStateStore

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DASHBOARD_DB", "data/events.db")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Execution System Dashboard", version="1.1")

# NOTE on handler style: DB-touching endpoints are deliberately plain `def`
# (not `async def`) so FastAPI runs them on its threadpool. They use
# blocking sqlite3 — inside `async def` they stalled the event loop under
# DB contention, which is what made the dashboard laggy / hang.

_state_store: Optional[RuntimeStateStore] = None
_command_queue: Optional[CommandQueue] = None
_position_store = None


def _stores() -> tuple[RuntimeStateStore, CommandQueue]:
    global _state_store, _command_queue
    if _state_store is None:
        _state_store = RuntimeStateStore(DB_PATH)
        _command_queue = CommandQueue(DB_PATH)
    return _state_store, _command_queue


def _positions():
    """Cached PositionStore (creating it per request re-ran schema DDL)."""
    global _position_store
    if _position_store is None:
        from src.storage.position_store import PositionStore
        _position_store = PositionStore(DB_PATH)
    return _position_store


class CommandRequest(BaseModel):
    type: str
    payload: dict = {}


@app.get("/")
async def index() -> FileResponse:
    # no-cache: browsers were serving a stale cached page after UI updates,
    # which made new tabs/tables appear "missing" until a hard refresh.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/state")
def get_state() -> dict:
    store, _ = _stores()
    state = store.read()
    if state is None:
        return {"status": "no_data", "positions": [], "orders": [],
                "strategy_pnl": {}, "system": {}}
    return state


@app.get("/api/events")
def get_events(limit: int = 50) -> list[dict]:
    """Recent events from the runner's EventStore table (read-only)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        cur = conn.execute(
            "SELECT timestamp, event_type, strategy_id, payload FROM events "
            "ORDER BY timestamp DESC LIMIT ?", (min(limit, 200),),
        )
        rows = [
            {"timestamp": r[0], "event_type": r[1], "strategy_id": r[2],
             "payload": json.loads(r[3]) if r[3] else {}}
            for r in cur.fetchall()
        ]
        conn.close()
        return rows
    except Exception as e:
        logger.error("events read failed: %s", e)
        return []


@app.get("/api/commands")
def get_commands(limit: int = 50) -> list[dict]:
    _, queue = _stores()
    return queue.recent(limit)


@app.post("/api/commands")
def post_command(req: CommandRequest) -> dict:
    if req.type not in VALID_COMMAND_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown command type: {req.type}")
    _, queue = _stores()
    command_id = queue.enqueue(req.type, req.payload)
    return {"command_id": command_id, "status": "pending"}


@app.websocket("/ws")
async def ws_state(ws: WebSocket) -> None:
    """Push the runtime snapshot every second."""
    await ws.accept()
    store, _ = _stores()
    try:
        while True:
            state = await asyncio.to_thread(store.read) or {"status": "no_data"}
            await ws.send_json(state)
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, Exception):
        return


@app.get("/api/logs")
def get_logs(lines: int = 100, errors_only: bool = False) -> list[str]:
    """Tail of the runner log (read-only file access, no broker)."""
    log_path = Path(os.environ.get("RUNNER_LOG", "data/runner.log"))
    if not log_path.exists():
        return []
    try:
        # Read the last ~256KB and split lines (cheap tail)
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 262144))
            text = f.read().decode(errors="replace")
        out = [l for l in text.splitlines() if l.strip()]
        if errors_only:
            out = [l for l in out if "ERROR" in l or "WARNING" in l or "CRITICAL" in l]
        return out[-min(lines, 500):]
    except Exception as e:
        logger.error("log tail failed: %s", e)
        return []


@app.get("/api/pnl")
def get_pnl() -> dict:
    """PnL summary: all-time per strategy, today (NY date), per-day breakdown."""
    store = _positions()
    per_strategy = store.strategy_pnl_summary()
    daily = store.daily_pnl_summary(days=30)
    # Merge live unrealized from the runtime snapshot
    state_store, _ = _stores()
    state = state_store.read() or {}
    live = state.get("strategy_pnl", {})
    out = {}
    for sid in set(per_strategy) | set(live):
        realized_hist = per_strategy.get(sid, {}).get("realized_pnl", 0.0)
        unreal = live.get(sid, {}).get("unrealized", 0.0)
        out[sid] = {
            "realized_all_time": realized_hist,
            "realized_today": daily["today"]["per_strategy"].get(sid, 0.0),
            "unrealized_live": unreal,
            "total": round(realized_hist + unreal, 2),
            "closed_positions": per_strategy.get(sid, {}).get("closed_positions", 0),
        }
    grand = round(sum(v["total"] for v in out.values()), 2)
    unreal_total = round(sum(v["unrealized_live"] for v in out.values()), 2)
    today_total = round(daily["today"]["realized_pnl"] + unreal_total, 2)
    return {
        "strategies": out,
        "total_pnl": grand,
        "today": {
            "date": daily["today"]["date"],
            "realized": daily["today"]["realized_pnl"],
            "unrealized": unreal_total,
            "total": today_total,
            "closed_positions": daily["today"]["closed_positions"],
            "per_strategy": daily["today"]["per_strategy"],
        },
        "daily": daily["days"],
    }


@app.get("/api/history")
def get_history(limit: int = 100) -> list[dict]:
    """Closed positions with realized PnL and close reason (SL/time/manual)."""
    return _positions().closed_positions(limit)
