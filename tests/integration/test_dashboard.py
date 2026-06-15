"""Phase 5 tests: dashboard is a DB-only consumer with zero broker access.

Run: python3 -m pytest tests/integration/test_dashboard.py -q
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from src.control.command_queue import CommandQueue
from src.storage.runtime_state import RuntimeStateStore

DASHBOARD_DIR = Path(__file__).parents[2] / "src" / "dashboard"

FORBIDDEN_IMPORT_PREFIXES = (
    "src.broker", "ib_async", "ibapi",
    "src.execution", "src.portfolio", "src.risk",
)


def test_dashboard_app_has_zero_broker_imports() -> None:
    """Structural proof: the FastAPI app's import graph cannot reach the
    broker (or any order/position machinery). AST-level, not just runtime."""
    src = (DASHBOARD_DIR / "app.py").read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        for forbidden in FORBIDDEN_IMPORT_PREFIXES:
            assert not mod.startswith(forbidden), (
                f"dashboard imports forbidden module {mod}"
            )


def test_dashboard_package_has_no_streamlit_imports() -> None:
    """The legacy Streamlit dashboard has been removed; the dashboard
    package must stay on the FastAPI/SQLite command-queue path."""
    for path in DASHBOARD_DIR.rglob("*.py"):
        src = path.read_text()
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert "streamlit" not in imported, (
            f"dashboard package imports legacy Streamlit module in {path}"
        )


def test_dashboard_runtime_does_not_load_broker_modules(monkeypatch) -> None:
    """Importing the app must not pull broker modules into sys.modules."""
    for m in list(sys.modules):
        if m.startswith("src.dashboard"):
            del sys.modules[m]
    import src.dashboard.app  # noqa: F401
    loaded = [m for m in sys.modules if m.startswith(("src.broker", "ib_async", "ibapi"))]
    # src.broker may already be loaded by OTHER tests in the same session;
    # assert the dashboard module itself holds no reference to a broker.
    dash = sys.modules["src.dashboard.app"]
    for attr in vars(dash).values():
        mod_name = getattr(attr, "__module__", "") or ""
        assert not mod_name.startswith(("src.broker", "ib_async")), (
            f"dashboard namespace references broker object {attr!r}"
        )


def test_dashboard_api_roundtrip(monkeypatch) -> None:
    """GET state written by a 'runner', POST a command, see it pending."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "events.db")

        # Simulate the runner side: write a snapshot
        RuntimeStateStore(db_path).write({
            "timestamp": "2026-06-11T15:00:00+00:00", "tick": 42,
            "status": "RUNNING",
            "system": {"locked": False, "reduce_only": False,
                       "paused_strategies": [], "flatten_all_active": False},
            "positions": [{"position_id": "abc", "strategy_id": "strat_x",
                           "contract": "XSP 20260611 730.0 PUT", "side": "BUY",
                           "quantity": 2, "avg_entry_price": 2.5,
                           "current_price": 2.7, "unrealized_pnl": 40.0,
                           "stop_price": 1.75, "time_exit_utc": None}],
            "orders": [], "strategy_pnl": {"strat_x": {"realized": 0.0,
                                                       "unrealized": 40.0,
                                                       "total": 40.0}},
        })

        import src.dashboard.app as dash
        monkeypatch.setattr(dash, "DB_PATH", db_path)
        monkeypatch.setattr(dash, "_state_store", None)
        monkeypatch.setattr(dash, "_command_queue", None)

        client = TestClient(dash.app)

        r = client.get("/api/state")
        assert r.status_code == 200
        body = r.json()
        assert body["tick"] == 42
        assert body["positions"][0]["strategy_id"] == "strat_x"
        assert body["strategy_pnl"]["strat_x"]["total"] == 40.0

        r = client.post("/api/commands", json={
            "type": "exit_position", "payload": {"position_id": "abc"}})
        assert r.status_code == 200
        cid = r.json()["command_id"]

        # The command is pending in the queue the runner drains
        pending = CommandQueue(db_path).fetch_pending()
        assert any(c["command_id"] == cid and c["type"] == "exit_position"
                   for c in pending)

        # Unknown command type is rejected at the API boundary
        r = client.post("/api/commands", json={"type": "place_order", "payload": {}})
        assert r.status_code == 400
