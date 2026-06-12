"""Dashboard view panels exports."""

from src.dashboard.views.status import render_status
from src.dashboard.views.positions import render_positions
from src.dashboard.views.orders import render_orders
from src.dashboard.views.strategies import render_strategies
from src.dashboard.views.risk import render_risk
from src.dashboard.views.analytics import render_analytics
from src.dashboard.views.logs import render_logs

__all__ = [
    "render_status",
    "render_positions",
    "render_orders",
    "render_strategies",
    "render_risk",
    "render_analytics",
    "render_logs",
]
