"""PositionManager for tracking active positions, entry/exit times, and PnL.

Position updates are driven primarily by FillEvents. Stop/target prices and
time exit rules can be updated for active positions.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID, uuid4

from src.core.enums import OrderSide, PositionStatus
from src.core.models import FillEvent, Position, QuoteSnapshot
from src.storage.event_log import EventStore

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages internal position states, driving changes from FillEvents.

    Isolated from BrokerClient and OrderManager.
    """

    def __init__(self, event_store: EventStore) -> None:
        """Initialize PositionManager.

        Args:
            event_store: EventStore for persisting audit logs.
        """
        self.event_store = event_store
        # Positions indexed by position_id
        self.positions: dict[UUID, Position] = {}

    def get_position(self, position_id: UUID) -> Optional[Position]:
        """Retrieve a position by its ID."""
        return self.positions.get(position_id)

    def get_open_positions(self) -> list[Position]:
        """Retrieve all currently open positions."""
        return [
            pos for pos in self.positions.values()
            if pos.status in (PositionStatus.OPENING, PositionStatus.OPEN)
        ]

    def handle_fill(self, fill: FillEvent, position_id: Optional[UUID] = None) -> Position:
        """Update position state based on a FillEvent.

        Args:
            fill: The fill event to process.
            position_id: Optional target position ID. If not provided, maps by
                open position matching strategy/contract, or generates a new one.
        """
        target_pos: Optional[Position] = None

        # 1. Try to find existing position
        if position_id is not None:
            target_pos = self.positions.get(position_id)
        else:
            # Look for an open position matching strategy and contract
            open_candidates = [
                pos for pos in self.get_open_positions()
                if pos.strategy_id == fill.strategy_id
                and pos.contract.symbol == fill.contract.symbol
                and pos.contract.strike == fill.contract.strike
                and pos.contract.right == fill.contract.right
                and pos.contract.expiry == fill.contract.expiry
            ]
            if open_candidates:
                target_pos = open_candidates[0]

        # 2. If no position exists, create a new one (Entry)
        if not target_pos:
            pid = position_id if position_id is not None else uuid4()
            target_pos = Position(
                position_id=pid,
                strategy_id=fill.strategy_id,
                contract=fill.contract,
                side=fill.side,
                quantity=fill.filled_quantity,
                filled_quantity=fill.filled_quantity,
                average_entry_price=fill.fill_price,
                status=PositionStatus.OPEN,
                entry_order_id=fill.order_id,
                entry_time=fill.timestamp,
                created_at=fill.timestamp,
                updated_at=fill.timestamp,
            )
            self.positions[target_pos.position_id] = target_pos

            self.event_store.log_callback("position_opened", {
                "position_id": target_pos.position_id,
                "strategy_id": target_pos.strategy_id,
                "contract": f"{target_pos.contract.symbol} {target_pos.contract.expiry} {target_pos.contract.strike} {target_pos.contract.right}",
                "side": target_pos.side.value,
                "quantity": target_pos.quantity,
                "entry_price": target_pos.average_entry_price,
                "timestamp": fill.timestamp.isoformat(),
            })
            return target_pos

        # 3. Existing position update
        old_qty = target_pos.quantity
        multiplier = fill.contract.multiplier if fill.contract.multiplier is not None else 100

        # If fill is on the same side, we are adding to the position (increase quantity)
        if fill.side == target_pos.side:
            new_qty = old_qty + fill.filled_quantity
            # Calculate weighted average entry price
            total_cost = (target_pos.average_entry_price * old_qty) + (fill.fill_price * fill.filled_quantity)
            target_pos.average_entry_price = total_cost / new_qty
            target_pos.quantity = new_qty
            target_pos.filled_quantity = new_qty
            target_pos.updated_at = fill.timestamp

            self.event_store.log_callback("position_added", {
                "position_id": target_pos.position_id,
                "added_quantity": fill.filled_quantity,
                "new_quantity": target_pos.quantity,
                "average_price": target_pos.average_entry_price,
                "timestamp": fill.timestamp.isoformat(),
            })
        else:
            # Opposite side: exit/reduction
            if fill.filled_quantity < old_qty:
                # Partial exit
                target_pos.quantity = old_qty - fill.filled_quantity
                # Calculate realized PnL on the exited portion
                if target_pos.side == OrderSide.BUY:
                    pnl = (fill.fill_price - target_pos.average_entry_price) * fill.filled_quantity * multiplier
                else:
                    pnl = (target_pos.average_entry_price - fill.fill_price) * fill.filled_quantity * multiplier

                target_pos.realized_pnl += pnl
                if fill.order_id not in target_pos.exit_order_ids:
                    target_pos.exit_order_ids.append(fill.order_id)
                target_pos.updated_at = fill.timestamp

                self.event_store.log_callback("position_partial_exit", {
                    "position_id": target_pos.position_id,
                    "exited_quantity": fill.filled_quantity,
                    "remaining_quantity": target_pos.quantity,
                    "exit_price": fill.fill_price,
                    "realized_pnl": pnl,
                    "timestamp": fill.timestamp.isoformat(),
                })
            else:
                # Full exit (fill quantity >= current position quantity)
                # Calculate realized PnL on remaining quantity
                exited_qty = old_qty
                if target_pos.side == OrderSide.BUY:
                    pnl = (fill.fill_price - target_pos.average_entry_price) * exited_qty * multiplier
                else:
                    pnl = (target_pos.average_entry_price - fill.fill_price) * exited_qty * multiplier

                target_pos.realized_pnl += pnl
                target_pos.quantity = 0
                target_pos.status = PositionStatus.CLOSED
                target_pos.exit_time = fill.timestamp
                if fill.order_id not in target_pos.exit_order_ids:
                    target_pos.exit_order_ids.append(fill.order_id)
                target_pos.updated_at = fill.timestamp

                self.event_store.log_callback("position_closed", {
                    "position_id": target_pos.position_id,
                    "exit_price": fill.fill_price,
                    "realized_pnl": target_pos.realized_pnl,
                    "timestamp": fill.timestamp.isoformat(),
                })

        return target_pos

    def force_close_position(self, position_id: UUID, exit_price: float, timestamp: datetime) -> Optional[Position]:
        """Forcefully close a position at a specific price (e.g. for emergency flattening).

        Args:
            position_id: The ID of the position to force close.
            exit_price: The simulated exit price.
            timestamp: The timestamp of the forced close.
        """
        pos = self.positions.get(position_id)
        if not pos or pos.status == PositionStatus.CLOSED:
            return pos

        multiplier = pos.contract.multiplier if pos.contract.multiplier is not None else 100
        # Calculate realized PnL on entire remaining quantity
        if pos.side == OrderSide.BUY:
            pnl = (exit_price - pos.average_entry_price) * pos.quantity * multiplier
        else:
            pnl = (pos.average_entry_price - exit_price) * pos.quantity * multiplier

        pos.realized_pnl += pnl
        pos.quantity = 0
        pos.status = PositionStatus.CLOSED
        pos.exit_time = timestamp
        pos.updated_at = timestamp

        self.event_store.log_callback("position_forced_closed", {
            "position_id": pos.position_id,
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "timestamp": timestamp.isoformat(),
        })

        return pos

    def set_exit_rules(
        self,
        position_id: UUID,
        stop_price: Optional[float] = None,
        target_price: Optional[float] = None,
        time_exit_utc: Optional[datetime] = None,
        use_mid_for_exits: bool = False,
    ) -> None:
        """Set stop loss, take profit, and time exit parameters for a position."""
        pos = self.positions.get(position_id)
        if pos:
            pos.stop_price = stop_price
            pos.target_price = target_price
            pos.time_exit_utc = time_exit_utc
            pos.use_mid_for_exits = use_mid_for_exits
            pos.updated_at = datetime.now(UTC)

    def update_position_prices(self, quotes: dict[str, QuoteSnapshot]) -> None:
        """Update current market price and unrealized PnL for open positions.

        Args:
            quotes: Map of symbol to QuoteSnapshot.
        """
        for pos in self.positions.values():
            if pos.status not in (PositionStatus.OPENING, PositionStatus.OPEN):
                continue

            opt_key = pos.contract.to_quote_symbol() if hasattr(pos.contract, "to_quote_symbol") else None
            quote = None
            if opt_key and opt_key in quotes:
                quote = quotes[opt_key]
            else:
                quote = quotes.get(pos.contract.symbol)

            if not quote:
                continue

            # Determine valuation price from QuoteSnapshot (prefer mid, otherwise bid/ask)
            price = None
            if quote.bid is not None and quote.ask is not None:
                price = (quote.bid + quote.ask) / 2.0
            elif quote.bid is not None:
                price = quote.bid
            elif quote.ask is not None:
                price = quote.ask

            if price is not None:
                pos.current_price = price
                multiplier = pos.contract.multiplier if pos.contract.multiplier is not None else 100
                if pos.side == OrderSide.BUY:
                    pos.unrealized_pnl = (price - pos.average_entry_price) * pos.quantity * multiplier
                else:
                    pos.unrealized_pnl = (pos.average_entry_price - price) * pos.quantity * multiplier
                pos.updated_at = datetime.now(UTC)
