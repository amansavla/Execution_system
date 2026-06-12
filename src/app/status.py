"""Runner status tracking for the execution system."""

from __future__ import annotations

from enum import Enum


class RunnerStatus(str, Enum):
    """Lifecycle states for the ExecutionRunner."""

    INITIALIZING = "INITIALIZING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
