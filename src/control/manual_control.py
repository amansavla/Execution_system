"""ManualControlService — central hub for all manual control commands.

Implements all required commands per AGENTS.md § "Manual control commands".
Routes actions through OrderManager and PositionManager — never calls
BrokerClient directly.

Every action is logged to EventStore as a manual_override event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

from src.control.overrides import OverrideManager
from src.core.models import ManualOverride, OrderState, Position
from src.execution.order_manager import OrderManager
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore

logger = logging.getLogger(__name__)


class ManualControlService:
    """Central service for operator manual controls.

    All manual commands go through this class. It never calls
    BrokerClient directly — flatten and cancel actions route through
    OrderManager and PositionManager.

    Dependencies are injected for full testability without a broker.
    """

    def __init__(
        self,
        event_store: EventStore,
        order_manager: OrderManager,
        position_manager: PositionManager,
        override_manager: OverrideManager,
        operator: str = "operator",
    ) -> None:
        """Initialize ManualControlService.

        Args:
            event_store: EventStore for logging all manual actions.
            order_manager: OrderManager for cancel/flatten order routing.
            position_manager: PositionManager for position queries and flattening.
            override_manager: OverrideManager for pause/resume/lock state.
            operator: Identifier for the human operator (for audit trail).
        """
        self.event_store = event_store
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.override_manager = override_manager
        self.operator = operator

    # ------------------------------------------------------------------
    # Logging helper
    # ------------------------------------------------------------------

    def _log_action(
        self,
        command: str,
        target: Optional[str] = None,
        parameters: Optional[dict] = None,
        result: Optional[str] = None,
    ) -> None:
        """Log a manual action to EventStore as manual_override event."""
        override = ManualOverride(
            command=command,
            target=target,
            parameters=parameters or {},
            operator=self.operator,
        )
        # Add result to the logged payload
        payload = override.model_dump(mode="json")
        if result is not None:
            payload["result"] = result
        self.event_store.log_callback("manual_override", payload)
        logger.info(
            "Manual control: command=%s target=%s operator=%s result=%s",
            command, target, self.operator, result,
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return system status summary.

        Returns:
            Dict with overrides state, open position count, active order count.
        """
        overrides = self.override_manager.state
        open_positions = self.position_manager.get_open_positions()
        active_orders = [
            o for o in self.order_manager.orders.values()
            if o.status.value not in ("FILLED", "CANCELLED", "REJECTED", "ERROR")
        ]

        status_info = {
            "system_locked": overrides.system_locked,
            "reduce_only": overrides.reduce_only,
            "paused_strategies": list(overrides.paused_strategies),
            "disabled_symbols": list(overrides.disabled_symbols),
            "reduce_only_strategies": list(overrides.reduce_only_strategies),
            "open_positions": len(open_positions),
            "active_orders": len(active_orders),
        }

        self._log_action("status", result="ok")
        return status_info

    # ------------------------------------------------------------------
    # Strategy pause/resume
    # ------------------------------------------------------------------

    def pause_strategy(self, strategy_id: str) -> str:
        """Pause a strategy from generating new entries."""
        changed = self.override_manager.pause_strategy(strategy_id)
        result = "paused" if changed else "already_paused"
        self._log_action("pause-strategy", target=strategy_id, result=result)
        return result

    def resume_strategy(self, strategy_id: str) -> str:
        """Resume a paused strategy."""
        changed = self.override_manager.resume_strategy(strategy_id)
        result = "resumed" if changed else "not_paused"
        self._log_action("resume-strategy", target=strategy_id, result=result)
        return result

    # ------------------------------------------------------------------
    # Symbol enable/disable
    # ------------------------------------------------------------------

    def disable_symbol(self, symbol: str) -> str:
        """Disable trading for a specific symbol."""
        changed = self.override_manager.disable_symbol(symbol)
        result = "disabled" if changed else "already_disabled"
        self._log_action("disable-symbol", target=symbol, result=result)
        return result

    def enable_symbol(self, symbol: str) -> str:
        """Re-enable a disabled symbol."""
        changed = self.override_manager.enable_symbol(symbol)
        result = "enabled" if changed else "not_disabled"
        self._log_action("enable-symbol", target=symbol, result=result)
        return result

    # ------------------------------------------------------------------
    # Reduce-only
    # ------------------------------------------------------------------

    def reduce_only(self, strategy_id: Optional[str] = None) -> str:
        """Set reduce-only mode globally or for a specific strategy."""
        changed = self.override_manager.set_reduce_only(True, strategy_id)
        target = strategy_id or "global"
        result = "reduce_only_enabled" if changed else "already_reduce_only"
        self._log_action("reduce-only", target=target, result=result)
        return result

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    def flatten_position(self, position_id: UUID, exit_price: float) -> str:
        """Force-close a specific position.

        Routes through PositionManager.force_close_position(), never
        through BrokerClient directly.

        Args:
            position_id: The position to flatten.
            exit_price: The price to mark the exit at.
        """
        pos = self.position_manager.force_close_position(
            position_id, exit_price, datetime.now(UTC)
        )
        if pos is None:
            result = "position_not_found_or_already_closed"
        else:
            result = "flattened"
        self._log_action(
            "flatten-position",
            target=str(position_id),
            parameters={"exit_price": exit_price},
            result=result,
        )
        return result

    def flatten_strategy(self, strategy_id: str, exit_price: float) -> str:
        """Force-close all open positions for a given strategy.

        Args:
            strategy_id: The strategy to flatten.
            exit_price: The exit price for all positions.
        """
        positions = [
            p for p in self.position_manager.get_open_positions()
            if p.strategy_id == strategy_id
        ]
        if not positions:
            result = "no_open_positions"
            self._log_action(
                "flatten-strategy",
                target=strategy_id,
                parameters={"exit_price": exit_price},
                result=result,
            )
            return result

        flattened = 0
        for pos in positions:
            self.position_manager.force_close_position(
                pos.position_id, exit_price, datetime.now(UTC)
            )
            flattened += 1

        result = f"flattened_{flattened}_positions"
        self._log_action(
            "flatten-strategy",
            target=strategy_id,
            parameters={"exit_price": exit_price, "count": flattened},
            result=result,
        )
        return result

    def flatten_all(self, exit_price: float) -> str:
        """Force-close all open positions across all strategies.

        Args:
            exit_price: The exit price for all positions.
        """
        positions = self.position_manager.get_open_positions()
        if not positions:
            result = "no_open_positions"
            self._log_action(
                "flatten-all",
                parameters={"exit_price": exit_price},
                result=result,
            )
            return result

        flattened = 0
        for pos in positions:
            self.position_manager.force_close_position(
                pos.position_id, exit_price, datetime.now(UTC)
            )
            flattened += 1

        result = f"flattened_{flattened}_positions"
        self._log_action(
            "flatten-all",
            parameters={"exit_price": exit_price, "count": flattened},
            result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Cancel orders
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: UUID) -> str:
        """Cancel a specific active order.

        Routes through OrderManager.cancel_order(), never through
        BrokerClient directly.

        Args:
            order_id: The order to cancel.
        """
        success = await self.order_manager.cancel_order(order_id)
        result = "cancelled" if success else "cancel_failed_or_not_found"
        self._log_action(
            "cancel-order",
            target=str(order_id),
            result=result,
        )
        return result

    async def cancel_all(self) -> str:
        """Cancel all active orders."""
        active_orders = [
            o for o in self.order_manager.orders.values()
            if o.status.value not in ("FILLED", "CANCELLED", "REJECTED", "ERROR")
        ]
        if not active_orders:
            result = "no_active_orders"
            self._log_action("cancel-all", result=result)
            return result

        cancelled = 0
        for order in active_orders:
            success = await self.order_manager.cancel_order(order.order_id)
            if success:
                cancelled += 1

        result = f"cancelled_{cancelled}_orders"
        self._log_action(
            "cancel-all",
            parameters={"count": cancelled},
            result=result,
        )
        return result

    # ------------------------------------------------------------------
    # Lock system
    # ------------------------------------------------------------------

    def lock_system(self) -> str:
        """Lock the system — prevents all new orders."""
        changed = self.override_manager.lock_system()
        result = "locked" if changed else "already_locked"
        self._log_action("lock-system", result=result)
        return result

    # ------------------------------------------------------------------
    # Show / query commands
    # ------------------------------------------------------------------

    def show_risk(self) -> dict:
        """Show current risk override state.

        Returns the current overrides state as a dict.
        """
        state = self.override_manager.state
        info = {
            "system_locked": state.system_locked,
            "reduce_only": state.reduce_only,
            "paused_strategies": list(state.paused_strategies),
            "disabled_symbols": list(state.disabled_symbols),
            "reduce_only_strategies": list(state.reduce_only_strategies),
        }
        self._log_action("show-risk", result="ok")
        return info

    def show_positions(self) -> list[dict]:
        """Show all open positions.

        Returns a list of position summaries.
        """
        positions = self.position_manager.get_open_positions()
        summaries = []
        for pos in positions:
            summaries.append({
                "position_id": str(pos.position_id),
                "strategy_id": pos.strategy_id,
                "contract": f"{pos.contract.symbol} {pos.contract.expiry} {pos.contract.strike} {pos.contract.right.value}",
                "side": pos.side.value,
                "quantity": pos.quantity,
                "entry_price": pos.average_entry_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "status": pos.status.value,
            })
        self._log_action("show-positions", result=f"{len(summaries)}_positions")
        return summaries

    def show_orders(self) -> list[dict]:
        """Show all active orders.

        Returns a list of order summaries.
        """
        active_statuses = {"NEW", "RISK_CHECKED", "SUBMITTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}
        orders = [
            o for o in self.order_manager.orders.values()
            if o.status.value in active_statuses
        ]
        summaries = []
        for order in orders:
            summaries.append({
                "order_id": str(order.order_id),
                "strategy_id": order.strategy_id,
                "contract": f"{order.contract.symbol} {order.contract.expiry} {order.contract.strike} {order.contract.right.value}",
                "side": order.side.value,
                "quantity": order.quantity,
                "filled_quantity": order.filled_quantity,
                "limit_price": order.limit_price,
                "status": order.status.value,
            })
        self._log_action("show-orders", result=f"{len(summaries)}_orders")
        return summaries

    def show_rejections(self) -> list[dict]:
        """Show recent risk rejection events from EventStore.

        Returns in-memory events of type 'risk_decision' that were not approved,
        or order_event with REJECTED status.
        """
        rejections = []
        for evt in self.event_store.events:
            if evt["type"] == "risk_decision":
                data = evt["data"]
                if isinstance(data, dict) and data.get("status") != "APPROVED":
                    rejections.append({
                        "type": "risk_rejection",
                        "timestamp": str(evt["timestamp"]),
                        "strategy_id": data.get("strategy_id"),
                        "status": data.get("status"),
                        "blocking_reasons": data.get("blocking_reasons", []),
                    })
            elif evt["type"] in ("order_event", "order_state_transition", "order_callback"):
                data = evt["data"]
                if isinstance(data, dict) and data.get("new_status") == "REJECTED":
                    rejections.append({
                        "type": "order_rejection",
                        "timestamp": str(evt["timestamp"]),
                        "order_id": data.get("order_id"),
                        "message": data.get("message", ""),
                    })

        self._log_action("show-rejections", result=f"{len(rejections)}_rejections")
        return rejections
