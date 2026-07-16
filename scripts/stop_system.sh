#!/bin/bash
# Clean shutdown for Execution_System_v2: graceful command first, force after.
set -u
cd "$(dirname "$0")/.."
say() { echo "[stop_system] $*"; }

RUNNER_PID=$(pgrep -f "run_paper_trading.py" | head -1 || true)
if [ -n "${RUNNER_PID:-}" ]; then
  say "Requesting graceful shutdown..."
  touch data/.shutdown_requested
  python3 - <<'PY'
import uuid, sqlite3
from datetime import datetime, timezone
con = sqlite3.connect("data/events.db", timeout=10)
con.execute("INSERT INTO commands (command_id,type,payload,status,created_at) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), "shutdown_runner", "{}", "pending", datetime.now(timezone.utc).isoformat()))
con.commit(); con.close()
PY
  for i in $(seq 1 8); do
    sleep 5
    pgrep -f "run_paper_trading.py" >/dev/null || break
  done
  if pgrep -f "run_paper_trading.py" >/dev/null; then
    say "Graceful shutdown not honored (hung runner) — force killing."
    pkill -TERM -f "run_paper_trading.py"; sleep 5
    pkill -9 -f "run_paper_trading.py" 2>/dev/null || true
    # the graceful command never ran; cancel it so it can't kill a future runner
    sqlite3 data/events.db "UPDATE commands SET status='done',
      result='cancelled by stop_system.sh (force kill used)'
      WHERE type='shutdown_runner' AND status='pending';" 2>/dev/null || true
  fi
  pkill -f "run_supervised.sh" 2>/dev/null || true
  say "Runner stopped."
else
  say "Runner not running."
fi

pkill -f "uvicorn src.dashboard.app" 2>/dev/null && say "Dashboard stopped." || say "Dashboard not running."
rm -f data/.shutdown_requested
say "Done."
