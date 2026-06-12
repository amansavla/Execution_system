"""XSP 0DTE Late-Afternoon Breakout Strategy.

Identical to XSPBreakoutStrategyProvider except:
- Reference price is the 12:00 PM NYC bar close (not 9:30 AM).
- Entry windows are afternoon: 1:00, 1:30, 2:00, 2:30 PM NYC.
"""

import logging
from datetime import date, datetime, time
from typing import Optional

from src.strategies.xsp_breakout import XSPBreakoutStrategyProvider
from src.broker.interface import BrokerClient
from src.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)

LATE_STRATEGY_PARAMS = {
    "xsp_late_1300": {"hour": 13, "minute": 0, "trigger_pct": 0.002},
    "xsp_late_1330": {"hour": 13, "minute": 30, "trigger_pct": 0.002},
    "xsp_late_1400": {"hour": 14, "minute": 0, "trigger_pct": 0.003},
    "xsp_late_1430": {"hour": 14, "minute": 30, "trigger_pct": 0.003},
}


class XSPBreakoutLateStrategyProvider(XSPBreakoutStrategyProvider):
    """Late-afternoon breakout variant using 12:00 PM NYC as the reference price."""

    STRATEGY_PARAMS = LATE_STRATEGY_PARAMS

    def _reference_bar_time(self, current_date: date, tz_ny) -> datetime:
        """12:00 PM bar close ends at 12:01 PM NY time."""
        return datetime.combine(current_date, time(12, 1), tzinfo=tz_ny)

    @property
    def _reference_label(self) -> str:
        return "12:00 PM"
