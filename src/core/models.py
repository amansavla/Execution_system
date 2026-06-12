"""Core Pydantic v2 models for the execution system.

All typed domain objects used across the system. No broker-specific
imports or logic. No dashboard files. Models enforce structural
validity at construction time.

See AGENTS.md § "Core models — required typed objects" for the
authoritative list.
"""

from datetime import UTC, datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from src.core.enums import (
    OptionRight,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _positive_int(v: int, field_name: str) -> int:
    """Validate that a quantity is a positive integer (> 0)."""
    if v <= 0:
        raise ValueError(f"{field_name} must be positive, got {v}")
    return v


# ---------------------------------------------------------------------------
# OptionContract
# ---------------------------------------------------------------------------

class OptionContract(BaseModel):
    """Describes an option contract without any broker-specific fields."""

    symbol: str = Field(..., min_length=1, description="Underlying symbol")
    expiry: str = Field(
        ...,
        pattern=r"^\d{8}$",
        description="Expiration date as YYYYMMDD string",
    )
    strike: float = Field(..., gt=0, description="Strike price")
    right: OptionRight = Field(..., description="CALL or PUT")
    multiplier: int = Field(default=100, gt=0, description="Contract multiplier")

    def to_quote_symbol(self) -> str:
        """Format contract as space-separated quote key."""
        strike_val = int(self.strike) if self.strike.is_integer() else self.strike
        right_char = self.right.value[0] if hasattr(self.right, "value") else str(self.right)[0]
        return f"{self.symbol} {self.expiry} {strike_val} {right_char}"


# ---------------------------------------------------------------------------
# QuoteSnapshot
# ---------------------------------------------------------------------------

class QuoteSnapshot(BaseModel):
    """A point-in-time quote for a contract or underlying.

    - Missing bid or ask is represented as None (detectable).
    - Stale quotes are detectable by comparing `timestamp` to current time.
    - bid > ask is rejected at construction.
    """

    symbol: str = Field(..., min_length=1)
    bid: Optional[float] = Field(default=None, description="Best bid, None if unavailable")
    ask: Optional[float] = Field(default=None, description="Best ask, None if unavailable")
    last: Optional[float] = Field(default=None, description="Last traded price")
    volume: Optional[int] = Field(default=None, ge=0)
    delta: Optional[float] = Field(default=None, description="Option delta, if available")
    close: Optional[float] = Field(default=None, description="Previous/today close price")
    timestamp: datetime = Field(..., description="Quote timestamp in UTC")

    @model_validator(mode="after")
    def _bid_not_greater_than_ask(self) -> "QuoteSnapshot":
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(
                f"bid ({self.bid}) must not be greater than ask ({self.ask})"
            )
        return self


# ---------------------------------------------------------------------------
# StrategySignal
# ---------------------------------------------------------------------------

class StrategySignal(BaseModel):
    """Emitted by a strategy. Contains no broker logic."""

    signal_id: UUID = Field(default_factory=uuid4)
    strategy_id: str = Field(..., min_length=1)
    direction: SignalDirection
    contract: OptionContract
    requested_quantity: int = Field(..., gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    take_profit_price: Optional[float] = Field(default=None, gt=0)
    time_exit_utc: Optional[datetime] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("requested_quantity")
    @classmethod
    def _validate_requested_quantity(cls, v: int) -> int:
        return _positive_int(v, "requested_quantity")


# ---------------------------------------------------------------------------
# AccountState
# ---------------------------------------------------------------------------

class AccountState(BaseModel):
    """Snapshot of account-level state for risk checks."""

    account_id: str = Field(..., min_length=1)
    net_liquidation: float = Field(..., ge=0)
    available_funds: float
    buying_power: float
    daily_pnl: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# RiskConfig
# ---------------------------------------------------------------------------

class RiskConfig(BaseModel):
    """Loaded from configs/risk.yaml. Typed representation."""

    daily_loss_limit: float = Field(..., gt=0)
    max_open_positions: int = Field(..., gt=0)
    max_open_orders: int = Field(..., gt=0)
    max_contracts_per_trade: int = Field(..., gt=0)
    max_premium_per_trade: float = Field(..., gt=0)
    max_spread_pct: float = Field(..., gt=0)
    quote_max_age_seconds: float = Field(..., gt=0)


# ---------------------------------------------------------------------------
# RiskDecision
# ---------------------------------------------------------------------------

class RiskDecision(BaseModel):
    """Output of RiskEngine for a given signal."""

    risk_decision_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    status: RiskDecisionStatus
    allowed_quantity: int = Field(..., ge=0)
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def approved(self) -> bool:
        return self.status == RiskDecisionStatus.APPROVED


# ---------------------------------------------------------------------------
# OrderIntent
# ---------------------------------------------------------------------------

class OrderIntent(BaseModel):
    """What the execution planner wants to do before broker translation."""

    order_intent_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    risk_decision_id: UUID
    position_id: Optional[UUID] = None
    is_entry: bool = True
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(..., gt=0)
    limit_price: float = Field(..., gt=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, v: int) -> int:
        return _positive_int(v, "quantity")


# ---------------------------------------------------------------------------
# OrderPlan
# ---------------------------------------------------------------------------

class OrderPlan(BaseModel):
    """Broker-neutral order plan ready for submission."""

    order_plan_id: UUID = Field(default_factory=uuid4)
    order_intent_id: UUID
    position_id: Optional[UUID] = None
    is_entry: bool = True
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(..., gt=0)
    order_type: str = Field(default="LMT")
    limit_price: float = Field(..., gt=0)
    time_in_force: str = Field(default="DAY")
    # Audit trail ref sent to the broker (IBKR orderRef):
    # {strategy}:{position_id|new}:{leg}:{side}:{unix_ms}
    order_ref: Optional[str] = None
    # Execution algo hint ("adaptive_urgent" etc.); broker applies when
    # eligible (single-leg, RTH), silently ignores otherwise.
    algo: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, v: int) -> int:
        return _positive_int(v, "quantity")


# ---------------------------------------------------------------------------
# OrderState
# ---------------------------------------------------------------------------

class OrderState(BaseModel):
    """Tracks the current state of an order through its lifecycle."""

    order_id: UUID = Field(default_factory=uuid4)
    order_plan_id: UUID
    position_id: Optional[UUID] = None
    is_entry: bool = True
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(..., gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    limit_price: float = Field(..., gt=0)
    # Original submit-time limit, preserved across reprices (for slippage measurement)
    first_limit_price: Optional[float] = None
    # Audit trail ref (same value submitted to the broker as orderRef)
    order_ref: Optional[str] = None
    # Replacement chain: set when the repricer cancels this order to
    # resubmit at a new price. An order with superseded_by is NOT a failed
    # leg — its outcome continues in the successor order.
    superseded_by: Optional[UUID] = None
    status: OrderStatus = Field(default=OrderStatus.NEW)
    broker_order_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, v: int) -> int:
        return _positive_int(v, "quantity")


# ---------------------------------------------------------------------------
# OrderEvent
# ---------------------------------------------------------------------------

class OrderEvent(BaseModel):
    """An event in the order lifecycle, logged to EventStore."""

    event_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    previous_status: Optional[OrderStatus] = None
    new_status: OrderStatus
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# FillEvent
# ---------------------------------------------------------------------------

class FillEvent(BaseModel):
    """Reported when a fill (full or partial) is received."""

    fill_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    filled_quantity: int = Field(..., gt=0)
    fill_price: float = Field(..., gt=0)
    commission: Optional[float] = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("filled_quantity")
    @classmethod
    def _validate_filled_quantity(cls, v: int) -> int:
        return _positive_int(v, "filled_quantity")


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class Position(BaseModel):
    """Internal position tracking, keyed by position_id + strategy_id."""

    position_id: UUID = Field(default_factory=uuid4)
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    quantity: int = Field(..., gt=0)
    filled_quantity: int = Field(default=0, ge=0)
    average_entry_price: float = Field(..., gt=0)
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: float = 0.0
    status: PositionStatus = Field(default=PositionStatus.OPENING)
    entry_order_id: UUID
    exit_order_ids: list[UUID] = Field(default_factory=list)
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    time_exit_utc: Optional[datetime] = None
    use_mid_for_exits: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict = Field(default_factory=dict)

    @field_validator("quantity")
    @classmethod
    def _validate_quantity(cls, v: int) -> int:
        return _positive_int(v, "quantity")


# ---------------------------------------------------------------------------
# ExitRule
# ---------------------------------------------------------------------------

class ExitRule(BaseModel):
    """Exit parameters owned by ExitManager after position creation."""

    position_id: UUID
    stop_price: Optional[float] = Field(default=None, gt=0)
    take_profit_price: Optional[float] = Field(default=None, gt=0)
    time_exit_utc: Optional[datetime] = None
    trailing_stop: bool = False
    trailing_stop_pct: Optional[float] = Field(default=None, gt=0)
    use_mid_for_exits: bool = False


# ---------------------------------------------------------------------------
# ManualOverride
# ---------------------------------------------------------------------------

class ManualOverride(BaseModel):
    """Represents a manual control action logged to EventStore."""

    override_id: UUID = Field(default_factory=uuid4)
    command: str = Field(..., min_length=1)
    target: Optional[str] = None
    parameters: dict = Field(default_factory=dict)
    operator: str = Field(default="system")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# ExecutionReport
# ---------------------------------------------------------------------------

class ExecutionReport(BaseModel):
    """Summary of execution quality for a completed order/position."""

    report_id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    strategy_id: str = Field(..., min_length=1)
    contract: OptionContract
    side: OrderSide
    requested_quantity: int = Field(..., gt=0)
    filled_quantity: int = Field(..., ge=0)
    average_fill_price: Optional[float] = None
    total_commission: Optional[float] = None
    slippage: Optional[float] = None
    fill_time_seconds: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# ReconciliationReport
# ---------------------------------------------------------------------------

class ReconciliationReport(BaseModel):
    """Output of ReconciliationEngine comparing internal vs broker state."""

    report_id: UUID = Field(default_factory=uuid4)
    matches: int = Field(default=0, ge=0)
    mismatches: int = Field(default=0, ge=0)
    internal_only: list[UUID] = Field(
        default_factory=list,
        description="Position IDs found internally but not at broker",
    )
    broker_only: list[str] = Field(
        default_factory=list,
        description="Broker position identifiers not tracked internally",
    )
    details: list[dict] = Field(default_factory=list)
    is_clean: bool = True
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
