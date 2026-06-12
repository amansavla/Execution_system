"""Core enumerations for the execution system.

All lifecycle states, directions, and modes used across the system.
No broker-specific imports or logic.
"""

from enum import Enum


class OrderStatus(str, Enum):
    """Order lifecycle states.

    NEW → RISK_CHECKED → SUBMITTED → PARTIALLY_FILLED → FILLED
                                    → CANCEL_PENDING → CANCELLED
                                    → REJECTED
                                    → ERROR
    """

    NEW = "NEW"
    RISK_CHECKED = "RISK_CHECKED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class PositionStatus(str, Enum):
    """Position lifecycle states.

    OPENING → OPEN → PARTIALLY_CLOSED → CLOSED → FORCE_CLOSED
    """

    OPENING = "OPENING"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    FORCE_CLOSED = "FORCE_CLOSED"


class OrderSide(str, Enum):
    """Buy or sell."""

    BUY = "BUY"
    SELL = "SELL"


class OptionRight(str, Enum):
    """Option type: call or put."""

    CALL = "CALL"
    PUT = "PUT"


class TradingMode(str, Enum):
    """System-wide trading mode."""

    PAPER = "PAPER"
    LIVE = "LIVE"
    DISABLED = "DISABLED"


class RiskDecisionStatus(str, Enum):
    """Outcome of a risk check."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SignalDirection(str, Enum):
    """Direction of a strategy signal."""

    LONG = "LONG"
    SHORT = "SHORT"
