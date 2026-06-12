"""MockBrokerClient implementation for simulating broker behavior.

Highly configurable MockBrokerClient that simulates:
- Connection and disconnection
- Order acceptance/rejection
- Delayed orders and fills
- Full and partial fills
- Order cancellations
- Stale quote generation
- Custom positions and account states
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from src.storage.event_log import EventStore

from src.broker.interface import BrokerClient
from src.core.enums import OrderStatus, PositionStatus
from src.core.models import (
    AccountState,
    FillEvent,
    OrderEvent,
    OrderPlan,
    OrderState,
    Position,
    QuoteSnapshot,
)


class MockBrokerConfig(BaseModel):
    """Configuration for MockBrokerClient simulation behavior."""

    connected_initially: bool = True

    # Order execution delays
    acceptance_delay_seconds: float = 0.0
    fill_delay_seconds: float = 0.0

    # Order outcomes
    auto_fill: bool = True
    reject_probability: float = 0.0  # Probability (0.0 to 1.0) of rejecting a new order
    reject_reason: str = "Simulated order rejection"

    # Partial fill configuration
    partial_fill_qty: Optional[int] = None
    partial_fill_delay_seconds: float = 0.0

    # Quote staleness
    stale_quote_age_seconds: float = 0.0

    # Preset / injected states for test overrides
    simulated_positions: list[Position] = Field(default_factory=list)
    simulated_open_orders: list[OrderState] = Field(default_factory=list)
    simulated_account_state: Optional[AccountState] = None
    simulated_quotes: dict[str, QuoteSnapshot] = Field(default_factory=dict)


@dataclass
class EventStoreStub:
    """DEPRECATED: Replaced by EventStore in Phase 8.

    Kept only for backward compatibility with tests that
    import this class directly. New code should use EventStore.
    """

    events: list[dict] = field(default_factory=list)

    def log_callback(self, callback_type: str, data: BaseModel) -> None:
        """Record an event callback."""
        self.events.append(
            {
                "type": callback_type,
                "data": data.model_dump() if hasattr(data, "model_dump") else data,
                "timestamp": datetime.now(UTC),
            }
        )


class MockBrokerClient(BrokerClient):
    """Configurable mock broker client for development and testing.

    Inherits from abstract BrokerClient and implements all its methods.
    Does not import or use any ib_async or strategy code.
    """

    def __init__(self, config: Optional[MockBrokerConfig] = None) -> None:
        self.config = config or MockBrokerConfig()
        self.connected = self.config.connected_initially
        self.event_store = EventStore()  # Phase 8: real EventStore replaces stub

        # In-memory database of active state
        self._orders: dict[str, OrderState] = {}
        self._dynamic_positions: list[Position] = []

        # Preset orders from config
        for order in self.config.simulated_open_orders:
            broker_id = order.broker_order_id or f"mock-order-{uuid4().hex[:8]}"
            order.broker_order_id = broker_id
            self._orders[broker_id] = order

        # Callbacks
        self._order_callbacks: list[Callable[[OrderState, OrderEvent], None]] = []
        self._fill_callbacks: list[Callable[[FillEvent], None]] = []
        self._quote_callbacks: list[Callable[[QuoteSnapshot], None]] = []

        # Background task tracking for tests to await if needed
        self.active_tasks: list[asyncio.Task] = []

    # ---------------------------------------------------------------
    # BrokerClient Connection Management
    # ---------------------------------------------------------------

    async def connect(self) -> None:
        """Simulate connecting to broker."""
        self.connected = True

    async def disconnect(self) -> None:
        """Simulate disconnecting from broker."""
        self.connected = False

    async def is_connected(self) -> bool:
        """Check connection state."""
        return self.connected

    # ---------------------------------------------------------------
    # Callback Registration
    # ---------------------------------------------------------------

    def register_order_callback(
        self, callback: Callable[[OrderState, OrderEvent], None]
    ) -> None:
        self._order_callbacks.append(callback)

    def register_fill_callback(self, callback: Callable[[FillEvent], None]) -> None:
        self._fill_callbacks.append(callback)

    def register_quote_callback(self, callback: Callable[[QuoteSnapshot], None]) -> None:
        self._quote_callbacks.append(callback)

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        """Fetch historical close price mock implementation."""
        if not self.connected:
            raise ConnectionError("MockBrokerClient disconnected")
        return getattr(self, "mock_historical_close", 500.0)

    # ---------------------------------------------------------------
    # Order Lifecycle APIs
    # ---------------------------------------------------------------

    async def place_order(self, order_plan: OrderPlan) -> OrderState:
        """Place order and trigger background simulation of lifecycle."""
        if not self.connected:
            raise ConnectionError("Cannot place order: Broker disconnected")

        broker_order_id = f"mock-order-{uuid4().hex[:8]}"

        # Initialize OrderState
        order_state = OrderState(
            order_plan_id=order_plan.order_plan_id,
            strategy_id=order_plan.strategy_id,
            contract=order_plan.contract,
            side=order_plan.side,
            quantity=order_plan.quantity,
            filled_quantity=0,
            limit_price=order_plan.limit_price,
            status=OrderStatus.NEW,
            broker_order_id=broker_order_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        self._orders[broker_order_id] = order_state

        # Spawn simulation task
        task = asyncio.create_task(self._simulate_order_lifecycle(broker_order_id))
        self.active_tasks.append(task)

        return order_state

    async def get_order_status(self, broker_order_id: str) -> Optional[OrderStatus]:
        """Ground-truth order status (mirrors IBKRBrokerClient.get_order_status)."""
        order = self._orders.get(broker_order_id)
        return order.status if order else None

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Request cancellation of an active order."""
        if not self.connected:
            raise ConnectionError("Cannot cancel order: Broker disconnected")

        order = self._orders.get(broker_order_id)
        if not order:
            return False

        # Can only cancel active orders
        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.ERROR,
        ):
            return False

        old_status = order.status
        order.status = OrderStatus.CANCELLED
        order.updated_at = datetime.now(UTC)

        # Emit cancel event
        event = OrderEvent(
            order_id=order.order_id,
            previous_status=old_status,
            new_status=OrderStatus.CANCELLED,
            message="Order cancelled by user",
            timestamp=datetime.now(UTC),
        )

        self._trigger_order_callbacks(order, event)
        return True

    # ---------------------------------------------------------------
    # Query APIs
    # ---------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Get positions.

        Returns simulated_positions if non-empty, otherwise dynamic ones.
        """
        if not self.connected:
            raise ConnectionError("Cannot fetch positions: Broker disconnected")

        if self.config.simulated_positions:
            return self.config.simulated_positions
        return self._dynamic_positions

    async def get_open_orders(self) -> list[OrderState]:
        """Get active orders."""
        if not self.connected:
            raise ConnectionError("Cannot fetch open orders: Broker disconnected")

        active_statuses = {
            OrderStatus.NEW,
            OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
        return [o for o in self._orders.values() if o.status in active_statuses]

    async def get_account_state(self) -> AccountState:
        """Get account state snapshot."""
        if not self.connected:
            raise ConnectionError("Cannot fetch account state: Broker disconnected")

        if self.config.simulated_account_state:
            return self.config.simulated_account_state

        return AccountState(
            account_id="MOCK_ACC_123",
            net_liquidation=100000.0,
            available_funds=80000.0,
            buying_power=400000.0,
            timestamp=datetime.now(UTC),
        )

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        """Get quote snapshot for symbols, adjusting timestamp for staleness."""
        if not self.connected:
            raise ConnectionError("Cannot fetch quotes: Broker disconnected")

        result: dict[str, QuoteSnapshot] = {}
        for sym in symbols:
            # Check preset quotes first
            if sym in self.config.simulated_quotes:
                base_quote = self.config.simulated_quotes[sym]
            else:
                # Default mock quote
                base_quote = QuoteSnapshot(
                    symbol=sym,
                    bid=100.0,
                    ask=101.0,
                    last=100.5,
                    volume=1000,
                    timestamp=datetime.now(UTC),
                )

            # Apply staleness offset if configured
            if self.config.stale_quote_age_seconds > 0:
                ts = base_quote.timestamp - timedelta(
                    seconds=self.config.stale_quote_age_seconds
                )
                # Re-construct to preserve validation
                quote = QuoteSnapshot(
                    symbol=base_quote.symbol,
                    bid=base_quote.bid,
                    ask=base_quote.ask,
                    last=base_quote.last,
                    volume=base_quote.volume,
                    close=base_quote.close,
                    timestamp=ts,
                )
            else:
                quote = base_quote

            result[sym] = quote

            # Trigger streaming callbacks
            for cb in self._quote_callbacks:
                try:
                    cb(quote)
                except Exception:
                    pass

        return result

    # ---------------------------------------------------------------
    # Internal lifecycle simulation
    # ---------------------------------------------------------------

    async def _simulate_order_lifecycle(self, broker_order_id: str) -> None:
        """Run async state transitions simulating real broker processing."""
        # 1. Acceptance Phase
        if self.config.acceptance_delay_seconds > 0:
            await asyncio.sleep(self.config.acceptance_delay_seconds)

        order = self._orders.get(broker_order_id)
        if not order or order.status == OrderStatus.CANCELLED:
            return

        # Check rejection probability
        import random

        if random.random() < self.config.reject_probability:
            old_status = order.status
            order.status = OrderStatus.REJECTED
            order.error_message = self.config.reject_reason
            order.updated_at = datetime.now(UTC)

            event = OrderEvent(
                order_id=order.order_id,
                previous_status=old_status,
                new_status=OrderStatus.REJECTED,
                message=self.config.reject_reason,
                timestamp=datetime.now(UTC),
            )
            self._trigger_order_callbacks(order, event)
            return

        # Transit to SUBMITTED
        old_status = order.status
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now(UTC)

        event = OrderEvent(
            order_id=order.order_id,
            previous_status=old_status,
            new_status=OrderStatus.SUBMITTED,
            message="Order accepted by mock broker exchange",
            timestamp=datetime.now(UTC),
        )
        self._trigger_order_callbacks(order, event)

        if not self.config.auto_fill:
            return

        # 2. Fill Phase
        if self.config.fill_delay_seconds > 0:
            await asyncio.sleep(self.config.fill_delay_seconds)

        order = self._orders.get(broker_order_id)
        if not order or order.status == OrderStatus.CANCELLED:
            return

        # Check for partial fill scenario
        p_qty = self.config.partial_fill_qty
        if p_qty is not None and p_qty < order.quantity:
            # Trigger Partial Fill
            old_status = order.status
            order.status = OrderStatus.PARTIALLY_FILLED
            order.filled_quantity = p_qty
            order.updated_at = datetime.now(UTC)

            # Order Status Update Event
            event = OrderEvent(
                order_id=order.order_id,
                previous_status=old_status,
                new_status=OrderStatus.PARTIALLY_FILLED,
                message=f"Partial fill executed: {p_qty} shares",
                timestamp=datetime.now(UTC),
            )
            self._trigger_order_callbacks(order, event)

            # Execution/Fill Event
            fill = FillEvent(
                order_id=order.order_id,
                strategy_id=order.strategy_id,
                contract=order.contract,
                side=order.side,
                filled_quantity=p_qty,
                fill_price=order.limit_price,
                commission=1.0,
                timestamp=datetime.now(UTC),
            )
            self._trigger_fill_callbacks(fill)
            self._update_dynamic_positions(order, p_qty)

            # Wait for remaining fill delay
            if self.config.partial_fill_delay_seconds > 0:
                await asyncio.sleep(self.config.partial_fill_delay_seconds)

            order = self._orders.get(broker_order_id)
            if not order or order.status == OrderStatus.CANCELLED:
                return

            # Final Fill Remaining
            old_status = order.status
            remaining_qty = order.quantity - p_qty
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.updated_at = datetime.now(UTC)

            event = OrderEvent(
                order_id=order.order_id,
                previous_status=old_status,
                new_status=OrderStatus.FILLED,
                message=f"Order fully filled. Final partial fill: {remaining_qty} shares",
                timestamp=datetime.now(UTC),
            )
            self._trigger_order_callbacks(order, event)

            fill = FillEvent(
                order_id=order.order_id,
                strategy_id=order.strategy_id,
                contract=order.contract,
                side=order.side,
                filled_quantity=remaining_qty,
                fill_price=order.limit_price,
                commission=1.0,
                timestamp=datetime.now(UTC),
            )
            self._trigger_fill_callbacks(fill)
            self._update_dynamic_positions(order, remaining_qty)

        else:
            # Full Fill immediately
            old_status = order.status
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.updated_at = datetime.now(UTC)

            event = OrderEvent(
                order_id=order.order_id,
                previous_status=old_status,
                new_status=OrderStatus.FILLED,
                message="Order filled completely",
                timestamp=datetime.now(UTC),
            )
            self._trigger_order_callbacks(order, event)

            fill = FillEvent(
                order_id=order.order_id,
                strategy_id=order.strategy_id,
                contract=order.contract,
                side=order.side,
                filled_quantity=order.quantity,
                fill_price=order.limit_price,
                commission=1.0,
                timestamp=datetime.now(UTC),
            )
            self._trigger_fill_callbacks(fill)
            self._update_dynamic_positions(order, order.quantity)

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _trigger_order_callbacks(self, order: OrderState, event: OrderEvent) -> None:
        """Fire callbacks and record in event store stub."""
        self.event_store.log_callback("order_callback", event)
        for cb in self._order_callbacks:
            try:
                cb(order, event)
            except Exception:
                pass

    def _trigger_fill_callbacks(self, fill: FillEvent) -> None:
        """Fire callbacks and record in event store stub."""
        self.event_store.log_callback("fill_callback", fill)
        for cb in self._fill_callbacks:
            try:
                cb(fill)
            except Exception:
                pass

    def _update_dynamic_positions(self, order: OrderState, qty: int) -> None:
        """Maintain simulated positions based on order executions."""
        # Find matching position
        match = None
        for p in self._dynamic_positions:
            if (
                p.strategy_id == order.strategy_id
                and p.contract.symbol == order.contract.symbol
                and p.contract.strike == order.contract.strike
                and p.contract.right == order.contract.right
                and p.contract.expiry == order.contract.expiry
            ):
                match = p
                break

        # Convert BUY/SELL to positive/negative impact
        side_mult = 1 if order.side == "BUY" else -1
        delta_qty = qty * side_mult

        if match:
            # Update existing position
            orig_net = match.quantity * (1 if match.side == "BUY" else -1)
            new_net = orig_net + delta_qty

            if new_net == 0:
                self._dynamic_positions.remove(match)
            else:
                match.quantity = abs(new_net)
                match.side = "BUY" if new_net > 0 else "SELL"
                match.status = PositionStatus.OPEN
                match.updated_at = datetime.now(UTC)
        else:
            # Create new position
            if delta_qty != 0:
                new_pos = Position(
                    strategy_id=order.strategy_id,
                    contract=order.contract,
                    side="BUY" if delta_qty > 0 else "SELL",
                    quantity=abs(delta_qty),
                    average_entry_price=order.limit_price,
                    entry_order_id=order.order_id,
                    status=PositionStatus.OPEN,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                self._dynamic_positions.append(new_pos)
