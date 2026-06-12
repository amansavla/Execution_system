"""Analytics module for execution quality and reporting."""

from src.analytics.execution_quality import (
    compute_execution_quality,
    compute_realized_pnl,
    normalize_events,
)
from src.analytics.reports import generate_text_report

__all__ = [
    "compute_execution_quality",
    "compute_realized_pnl",
    "normalize_events",
    "generate_text_report",
]
