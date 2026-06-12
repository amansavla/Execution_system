#!/bin/bash
# Supervised runner: relaunches run_paper_trading.py after crashes or
# dashboard-initiated restarts. A dashboard "Shutdown" (or creating
# data/.shutdown_requested) ends the loop for good.
#
# Usage:  nohup ./scripts/run_supervised.sh >> data/supervisor.log 2>&1 &
cd "$(dirname "$0")/.." || exit 1
rm -f data/.shutdown_requested

while true; do
    echo "[supervisor] $(date '+%Y-%m-%d %H:%M:%S') starting runner"
    python3 run_paper_trading.py >> data/runner.log 2>&1
    code=$?
    if [ -f data/.shutdown_requested ]; then
        echo "[supervisor] shutdown requested — exiting"
        rm -f data/.shutdown_requested
        break
    fi
    echo "[supervisor] runner exited (code $code) — restarting in 5s"
    sleep 5
done
