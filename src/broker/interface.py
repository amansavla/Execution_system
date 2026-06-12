"""BrokerClient abstract base class interface.

All broker clients (including MockBrokerClient and IBKRBrokerClient)
must inherit from this class and implement its interface fully.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from src.core.models import (
    AccountState,
    FillEvent,
    OrderEvent,
    OrderPlan,
    OrderState,
    Position,
    QuoteSnapshot,
)


class BrokerClient(ABC):
    """Abstract Broker Client interface.

    Defines connection, order execution, querying, and event callback APIs.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the broker."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        pass

    @abstractmethod
    async def is_connected(self) -> bool:
        """Check if client is currently connected to the broker."""
        pass

    @abstractmethod
    async def place_order(self, order_plan: OrderPlan) -> OrderState:
        """Submit a new order.

        Returns the initial OrderState (usually NEW or SUBMITTED).
        Asynchronous updates should trigger order and fill callbacks.
        """
        pass

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Request cancellation of an active order by broker_order_id.

        Returns True if the cancellation request was sent successfully.
        """
        pass

    async def modify_order(self, broker_order_id: str, new_limit_price: float) -> bool:
        """Modify a working limit order's price in place (no cancel/replace).

        Default implementation returns False (not supported); brokers that
        support in-place amendment override this. Callers must fall back to
        cancel/replace when this returns False.
        """
        return False

    def get_latest_completed_bar(self, symbol: str):
        """Most recent completed 1-min bar for symbol, or None.

        Default: None (no bar support). Brokers with streaming data
        override this.
        """
        return None

    def get_completed_bars(self, symbol: str, count: int = 60) -> list:
        """Up to `count` most recent completed 1-min bars (oldest first)."""
        return []

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch all active positions from the broker."""
        pass

    @abstractmethod
    async def get_open_orders(self) -> list[OrderState]:
        """Fetch all active/open orders from the broker."""
        pass

    @abstractmethod
    async def get_account_state(self) -> AccountState:
        """Fetch current account balances and buying power from the broker."""
        pass

    @abstractmethod
    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        """Fetch current quotes for the specified symbols."""
        pass

    # Callback Registration
    @abstractmethod
    def register_order_callback(
        self, callback: Callable[[OrderState, OrderEvent], None]
    ) -> None:
        """Register a callback for order state updates."""
        pass

    @abstractmethod
    def register_fill_callback(self, callback: Callable[[FillEvent], None]) -> None:
        """Register a callback for order execution/fill events."""
        pass

    @abstractmethod
    def register_quote_callback(self, callback: Callable[[QuoteSnapshot], None]) -> None:
        """Register a callback for streaming quote updates."""
        pass

    @abstractmethod
    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        """Fetch historical close price for a symbol ending at the specified datetime."""
        pass
