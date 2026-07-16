#!/bin/bash
# One-command starter for Execution_System_v2 paper trading.
#
# Safe to run any time (idempotent). Handles every startup gotcha hit in
# live sessions so a human doesn't have to remember them:
#   - waits for TWS (port 7497) instead of failing
#   - kills a stale runner left over from a PREVIOUS day (overnight zombies
#     hung on dead TWS connections: 2026-07-01, 07-14->15, 07-15->16)
#   - clears a stale data/.shutdown_requested flag (blocks the supervisor)
#   - cancels stale pending shutdown/restart commands in the queue (a new
#     runner consumed one and instantly shut itself down, 2026-07-15)
#   - starts runner + dashboard only if not already running today
#   - opens the dashboard in the browser
#
# Usage: ./scripts/start_system.sh        (or double-click the Desktop
#        "Start Trading System.command")

set -u
cd "$(dirname "$0")/.."
REPO="$(pwd)"
DB="data/events.db"
TODAY_TAG=$(date "+%a %b %e")   # matches `ps lstart` prefix, e.g. "Thu Jul 16"

say() { echo "[start_system] $*"; }

# ---------------------------------------------------------------- TWS gate
if ! nc -z -w2 127.0.0.1 7497 2>/dev/null; then
  say "TWS/IB Gateway is not running (port 7497 closed)."
  say "Please start TWS and log in to the PAPER account. Waiting up to 5 minutes..."
  for i in $(seq 1 30); do
    sleep 10
    if nc -z -w2 127.0.0.1 7497 2>/dev/null; then break; fi
    if [ "$i" = "30" ]; then say "Gave up waiting for TWS. Start it and re-run."; exit 1; fi
  done
fi
say "TWS reachable on :7497."

# ------------------------------------------- stale-runner / zombie cleanup
RUNNER_PID=$(pgrep -f "run_paper_trading.py" | head -1 || true)
if [ -n "${RUNNER_PID:-}" ]; then
  STARTED=$(ps -o lstart= -p "$RUNNER_PID" | sed 's/^ *//')
  if [[ "$STARTED" == "$TODAY_TAG"* ]]; then
    say "Runner already running (pid $RUNNER_PID, started today). Leaving it alone."
    ALREADY_RUNNING=1
  else
    say "Found STALE runner from a previous day (pid $RUNNER_PID, started $STARTED). Stopping it."
    touch data/.shutdown_requested          # tell supervisor not to respawn
    kill -TERM "$RUNNER_PID" 2>/dev/null
    sleep 6
    kill -9 "$RUNNER_PID" 2>/dev/null || true
    pkill -f "run_supervised.sh" 2>/dev/null || true
    sleep 2
  fi
fi

# ------------------------------------------------------- stale-state clear
rm -f data/.shutdown_requested
CLEARED=$(sqlite3 "$DB" "UPDATE commands SET status='done',
  result='stale command cancelled by start_system.sh'
  WHERE type IN ('shutdown_runner','restart_runner','fire_straddle')
    AND status='pending';
  SELECT changes();" 2>/dev/null || echo 0)
[ "${CLEARED:-0}" != "0" ] && say "Cancelled $CLEARED stale pending shutdown/restart command(s)."

# ------------------------------------------------------------------ runner
if [ -z "${ALREADY_RUNNING:-}" ]; then
  say "Starting runner (supervised)..."
  nohup ./scripts/run_supervised.sh >> data/supervisor.log 2>&1 &
  sleep 3
  RUNNER_PID=$(pgrep -f "run_paper_trading.py" | head -1 || true)
  if [ -z "$RUNNER_PID" ]; then say "Runner failed to start — check data/supervisor.log"; exit 1; fi
  say "Runner up (pid $RUNNER_PID). Warmup takes ~60-90s (bar history + reconciliation)."
fi

# --------------------------------------------------------------- dashboard
if ! pgrep -f "uvicorn src.dashboard.app" >/dev/null; then
  say "Starting dashboard on :8500..."
  nohup python3 -m uvicorn src.dashboard.app:app --port 8500 >> data/dashboard.log 2>&1 &
  sleep 3
else
  say "Dashboard already running."
fi

# ------------------------------------------------------------------ finish
open "http://localhost:8500" 2>/dev/null || true
say "Done. Dashboard: http://localhost:8500  (Fire Straddle button lives there)"
say "Logs: tail -f data/runner.log"
