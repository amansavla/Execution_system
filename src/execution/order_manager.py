"""OrderManager for handling limit order lifecycle, callbacks, and repricing.

Tracks order status transitions, manages partial fills, protects against
duplicate order submissions, and implements configurable repricing logic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Callable, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from src.broker.interface import BrokerClient
from src.core.enums import OrderSide, OrderStatus
from src.core.models import (
    FillEvent,
    OrderEvent,
    OrderIntent,
    OrderPlan,
    OrderState,
    RiskDecision,
)
from src.storage.event_log import EventStore


class RepriceConfig(BaseModel):
    """Configuration options for limit order repricing behavior."""

    enabled: bool = False
    max_attempts: int = 3
    reprice_interval_seconds: float = 5.0
    max_acceptable_buy_price: float = 999999.0
    min_acceptable_sell_price: float = 0.01
    timeout_seconds: float = 30.0
    # After this many attempts, price THROUGH the touch by
    # cross_touch_offset (marketable-plus limit: buy above ask / sell
    # below bid). A limit slightly through the NBBO is accepted by IBKR
    # and fills like a protected market order — it protects against the
    # quote moving between our read and the order's arrival. None = never
    # cross (entries keep this default; urgent exits set it).
    cross_touch_after_attempts: Optional[int] = None
    cross_touch_offset: float = 0.05
    # In-place IBKR order modification proved fragile on 0DTE options
    # (Error 202/201 "unapproved modification" -> the resting order is
    # cancelled -> read as a spontaneous cancel -> fail-closed lock, 3x on
    # 2026-06-11). Default OFF: use the safe cancel/replace path, which sets
    # CANCEL_PENDING first so the follow-up cancel is expected, not spontaneous.
    use_in_place_modify: bool = False


class OrderManager:
    """Manages order submission, tracking, cancellation, and repricing.

    Communicates with BrokerClient and logs events to EventStore.
    Strictly isolated from RiskEngine and PositionManager.
    """

    def __init__(self, broker: BrokerClient, event_store: EventStore) -> None:
        """Initialize OrderManager.

        Args:
            broker: Broker client interface implementation.
            event_store: EventStore for persisting audit logs.
        """
        self.broker = broker
        self.event_store = event_store
        self.orders: dict[UUID, OrderState] = {}
        self.broker_rejected_order_ids: set[UUID] = set()
        # asyncio holds only weak references to running tasks; without a
        # strong reference the GC can destroy a pending repricer mid-flight
        # ("Task was destroyed but it is pending" -> exits never chased).
        self._background_tasks: set[asyncio.Task] = set()

        # Register callbacks
        self.broker.register_order_callback(self._on_broker_order_update)
        self.broker.register_fill_callback(self._on_broker_fill)

    def _spawn(self, coro) -> asyncio.Task:
        """create_task with a strong reference until the task completes."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ---------------------------------------------------------------
    # Submission & Control
    # ---------------------------------------------------------------

    async def submit_intent(
        self,
        intent: OrderIntent,
        decision: RiskDecision,
        emergency_flatten: bool = False,
        reprice_config: Optional[RepriceConfig] = None,
        algo: Optional[str] = None,
    ) -> OrderState:
        """Submit an OrderIntent with an approved RiskDecision.

        Args:
            intent: The execution planner order intent.
            decision: The approved RiskDecision matching the intent.
            emergency_flatten: If True, uses MKT order instead of LMT.
            reprice_config: Config options for chasing quote price.
        """
        # 1. Verification gates
        if not decision.approved:
            raise ValueError(
                f"Cannot submit OrderIntent {intent.order_intent_id}: "
                f"RiskDecision is not APPROVED"
            )

        if decision.risk_decision_id != intent.risk_decision_id:
            raise ValueError(
                f"Cannot submit OrderIntent {intent.order_intent_id}: "
                f"RiskDecision ID mismatch"
            )

        # 2. Duplicate order check for same position_id
        if intent.position_id is not None:
            for active_order in self.orders.values():
                if (
                    active_order.position_id == intent.position_id
                    and active_order.status
                    in (
                        OrderStatus.NEW,
                        OrderStatus.RISK_CHECKED,
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.CANCEL_PENDING,
                    )
                ):
                    raise ValueError(
                        f"Duplicate order submission rejected: "
                        f"Active order {active_order.order_id} already exists "
                        f"for position {intent.position_id}"
                    )

        # 3. Build the audit orderRef at CONSTRUCTION time:
        #    {strategy}:{position_id|new}:{leg}:{side}:{unix_ms}
        #    leg = CE/PE from the option right (deterministic for both
        #    automated and dashboard-originated orders — the test key).
        right = getattr(intent.contract, "right", None)
        right_val = right.value if hasattr(right, "value") else str(right or "")
        leg = "CE" if "CALL" in right_val.upper() or right_val.upper() == "C" else "PE"
        unix_ms = int(datetime.now(UTC).timestamp() * 1000)
        order_ref = (
            f"{intent.strategy_id}:{intent.position_id or 'new'}:"
            f"{leg}:{intent.side.value}:{unix_ms}"
        )

        # Create OrderPlan (neutral format)
        order_plan = OrderPlan(
            order_intent_id=intent.order_intent_id,
            position_id=intent.position_id,
            is_entry=intent.is_entry,
            strategy_id=intent.strategy_id,
            contract=intent.contract,
            side=intent.side,
            quantity=decision.allowed_quantity,  # clamp to risk limit
            order_type="MKT" if emergency_flatten else "LMT",
            limit_price=intent.limit_price,
            order_ref=order_ref,
            algo=algo,
            timestamp=datetime.now(UTC),
        )

        # 4. Create OrderState starting at NEW
        order_state = OrderState(
            order_plan_id=order_plan.order_plan_id,
            position_id=order_plan.position_id,
            is_entry=order_plan.is_entry,
            strategy_id=order_plan.strategy_id,
            contract=order_plan.contract,
            side=order_plan.side,
            quantity=order_plan.quantity,
            filled_quantity=0,
            limit_price=order_plan.limit_price,
            first_limit_price=order_plan.limit_price,
            order_ref=order_ref,
            status=OrderStatus.NEW,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        if algo:
            order_state.metadata["algo"] = algo
        # Entry-batch tag: multi-leg coordination only correlates legs of
        # the SAME submission batch. Without it a failed leg was matched
        # against every later order of the strategy that day, aborting all
        # subsequent re-entry cycles (GTH live test 2026-06-11).
        batch = (intent.metadata or {}).get("entry_batch")
        if batch:
            order_state.metadata["entry_batch"] = batch
        self.orders[order_state.order_id] = order_state

        self._log_event(
            order_state.order_id,
            None,
            OrderStatus.NEW,
            "Order state created from intent",
        )

        # 5. Transition to RISK_CHECKED
        order_state.status = OrderStatus.RISK_CHECKED
        order_state.updated_at = datetime.now(UTC)
        self._log_event(
            order_state.order_id,
            OrderStatus.NEW,
            OrderStatus.RISK_CHECKED,
            "Order risk check validated",
        )

        # 6. Submit to BrokerClient
        try:
            broker_order = await self.broker.place_order(order_plan)
            order_state.broker_order_id = broker_order.broker_order_id
            order_state.status = broker_order.status
            order_state.updated_at = datetime.now(UTC)

            self._log_event(
                order_state.order_id,
                OrderStatus.RISK_CHECKED,
                order_state.status,
                f"Order submitted to broker client with ID {order_state.broker_order_id}",
            )
        except Exception as e:
            order_state.status = OrderStatus.ERROR
            order_state.error_message = str(e)
            order_state.updated_at = datetime.now(UTC)
            self._log_event(
                order_state.order_id,
                OrderStatus.RISK_CHECKED,
                OrderStatus.ERROR,
                f"Broker submission failed: {e}",
            )
            return order_state

        # 7. Repricer initialization
        if reprice_config and reprice_config.enabled and not emergency_flatten:
            self._spawn(
                self._run_repricer(order_state.order_id, reprice_config)
            )

        return order_state

    async def cancel_order(self, order_id: UUID, hard_cancel: bool = True) -> bool:
        """Cancel an active order."""
        order = self.orders.get(order_id)
        if not order:
            return False

        if hard_cancel:
            order.metadata["hard_cancel"] = True

        if order.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.ERROR,
        ):
            return False

        old_status = order.status
        order.status = OrderStatus.CANCEL_PENDING
        order.updated_at = datetime.now(UTC)

        self._log_event(
            order.order_id,
            old_status,
            OrderStatus.CANCEL_PENDING,
            "Order cancellation requested",
        )

        # If not sent to broker yet
        if not order.broker_order_id:
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now(UTC)
            self._log_event(
                order.order_id,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELLED,
                "Cancelled locally (no broker ID)",
            )
            return True

        try:
            return await self.broker.cancel_order(order.broker_order_id)
        except Exception as e:
            order.status = OrderStatus.ERROR
            order.error_message = f"Cancellation failed: {e}"
            order.updated_at = datetime.now(UTC)
            self._log_event(
                order.order_id,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.ERROR,
                f"Cancellation broker call failed: {e}",
            )
            return False

    async def resolve_stuck_cancels(self, max_age_seconds: float = 20.0) -> None:
        """Resolve orders stuck in CANCEL_PENDING.

        Live incident 2026-06-11: cancel confirmations from IBKR were
        occasionally never received, leaving orders in CANCEL_PENDING for
        180s+ — invisible to the entry-timeout sweep AND to multi-leg
        coordination (CANCEL_PENDING is deliberately not a failure signal),
        so straddle legs sat in limbo. This sweep queries the broker's
        ground truth and either adopts the terminal status or re-issues
        the cancel.
        """
        now = datetime.now(UTC)
        query = getattr(self.broker, "get_order_status", None)
        for order in list(self.orders.values()):
            if order.status != OrderStatus.CANCEL_PENDING:
                continue
            age = (now - order.updated_at).total_seconds()
            if age < max_age_seconds:
                continue

            broker_status = None
            if query is not None and order.broker_order_id:
                try:
                    broker_status = await query(order.broker_order_id)
                except Exception as e:
                    self._log_event(
                        order.order_id, order.status, order.status,
                        f"Stuck-cancel sweep: status query failed: {e}",
                    )
                    continue

            if broker_status in (
                OrderStatus.CANCELLED, OrderStatus.FILLED,
                OrderStatus.REJECTED, OrderStatus.ERROR,
            ):
                # Confirmation callback was lost — adopt broker truth.
                old = order.status
                order.status = broker_status
                order.updated_at = now
                self._log_event(
                    order.order_id, old, broker_status,
                    f"Stuck-cancel sweep: adopted broker status after {age:.0f}s in CANCEL_PENDING",
                )
            elif broker_status is None:
                # Broker does not know the order: nothing can fill us.
                old = order.status
                order.status = OrderStatus.CANCELLED
                order.updated_at = now
                self._log_event(
                    order.order_id, old, OrderStatus.CANCELLED,
                    f"Stuck-cancel sweep: order unknown to broker after {age:.0f}s; marked cancelled",
                )
            else:
                # Still working at the broker — our cancel was lost. Re-issue.
                self._log_event(
                    order.order_id, order.status, order.status,
                    f"Stuck-cancel sweep: still {broker_status} at broker after {age:.0f}s; re-issuing cancel",
                )
                try:
                    await self.broker.cancel_order(order.broker_order_id)
                    order.updated_at = now  # restart the age clock
                except Exception as e:
                    self._log_event(
                        order.order_id, order.status, order.status,
                        f"Stuck-cancel sweep: re-cancel failed: {e}",
                    )

    # ---------------------------------------------------------------
    # Broker Callbacks
    # ---------------------------------------------------------------

    def _on_broker_order_update(
        self, broker_order: OrderState, event: OrderEvent
    ) -> None:
        """Process incoming order updates from broker."""
        order = self._find_order_by_broker_id(broker_order.broker_order_id)
        if not order:
            return

        old_status = order.status

        # Stale/duplicate callbacks: if the order is already in a terminal
        # state (FILLED, CANCELLED, REJECTED, ERROR), a late "Cancelled"
        # callback racing with a fill confirmation is a known IBKR artifact,
        # not a real broker-initiated cancellation. Log it and ignore it —
        # do not mutate order state and do not trigger the fail-closed lock.
        terminal_statuses = (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.ERROR,
        )
        if old_status in terminal_statuses:
            self._log_event(
                order.order_id,
                old_status,
                old_status,
                f"Ignored stale broker update callback (order already {old_status}): {event.message}",
            )
            return

        # Benign modify-vs-cancel race (live incident 2026-06-11): a reprice
        # modify can land just as the order is being cancelled; IBKR answers
        # Error 201 "too late to replace" / "Prior submit/modify was
        # rejected". The ORDER itself ends Cancelled as WE requested — this
        # is not a broker-initiated reject, so it must not trip Hard Rule 7.
        if (
            old_status == OrderStatus.CANCEL_PENDING
            and broker_order.status in (OrderStatus.REJECTED, OrderStatus.ERROR)
        ):
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now(UTC)
            self._log_event(
                order.order_id,
                old_status,
                OrderStatus.CANCELLED,
                f"Reject during our own cancellation treated as cancelled "
                f"(modify/cancel race): {event.message}",
            )
            return

        if old_status == OrderStatus.CANCEL_PENDING and broker_order.status in (
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.NEW,
        ):
            order.status = OrderStatus.CANCEL_PENDING
        else:
            order.status = broker_order.status
        order.filled_quantity = broker_order.filled_quantity
        order.error_message = broker_order.error_message
        order.updated_at = datetime.now(UTC)

        self._log_event(
            order.order_id,
            old_status,
            order.status,
            f"Broker update callback: {event.message}",
        )

        # Check for broker rejection or spontaneous cancellation (fail-closed trigger)
        is_spontaneous_cancel = (
            broker_order.status == OrderStatus.CANCELLED
            and old_status != OrderStatus.CANCEL_PENDING
        )
        is_reject_or_error = broker_order.status in (OrderStatus.REJECTED, OrderStatus.ERROR)

        if is_spontaneous_cancel or is_reject_or_error:
            self.broker_rejected_order_ids.add(order.order_id)

        # Check partial entry fill cancel remainder rule
        if (
            order.status == OrderStatus.PARTIALLY_FILLED
            and order.is_entry
        ):
            self._log_event(
                order.order_id,
                order.status,
                order.status,
                "Partial fill detected on entry order; canceling remainder",
            )
            self._spawn(self.cancel_order(order.order_id))

    def _on_broker_fill(self, fill: FillEvent) -> None:
        """Process incoming fill executions for logs/audits."""
        self.event_store.log_callback("fill_received", fill)

        # Execution-quality instrumentation: compare the fill against the
        # order's ORIGINAL submit limit (pre-reprice) and record time-to-fill
        # and reprice count. Feeds the daily slippage/tracking-error report.
        order = self.orders.get(fill.order_id)
        if order is None:
            # Fill may reference the broker-side order id; find by match
            for o in self.orders.values():
                if o.broker_order_id and str(fill.metadata.get("broker_order_id")) == o.broker_order_id:
                    order = o
                    break
        if order is not None:
            first_limit = getattr(order, "first_limit_price", None) or order.limit_price
            slippage = None
            if first_limit:
                # Positive slippage = paid more (BUY) / received less (SELL)
                if order.side == OrderSide.BUY:
                    slippage = round(fill.fill_price - first_limit, 4)
                else:
                    slippage = round(first_limit - fill.fill_price, 4)
            time_to_fill = (fill.timestamp - order.created_at).total_seconds()
            self.event_store.log_callback("execution_quality", {
                "order_id": str(order.order_id),
                "is_entry": order.is_entry,
                "strategy_id": order.strategy_id,
                "side": order.side.value,
                "first_limit_price": first_limit,
                "final_limit_price": order.limit_price,
                "fill_price": fill.fill_price,
                "filled_quantity": fill.filled_quantity,
                "slippage_vs_first_limit": slippage,
                "time_to_fill_seconds": round(time_to_fill, 3),
                "timestamp": fill.timestamp.isoformat(),
            })

    # ---------------------------------------------------------------
    # Repricing loop
    # ---------------------------------------------------------------

    async def _run_repricer(self, order_id: UUID, config: RepriceConfig) -> None:
        """Asynchronously monitor quote and reprice limit order if unfilled."""
        start_time = datetime.now(UTC)
        attempts = 0
        current_order_id = order_id

        while True:
            await asyncio.sleep(config.reprice_interval_seconds)

            order = self.orders.get(current_order_id)
            if not order or order.metadata.get("hard_cancel"):
                break

            # Done / terminated states
            if order.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.ERROR,
            ):
                break

            # Timeout check
            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            if elapsed >= config.timeout_seconds:
                await self.cancel_order(current_order_id)
                self._log_event(
                    current_order_id,
                    order.status,
                    order.status,
                    f"Reprice timed out after {elapsed:.1f} seconds",
                )
                break

            # Max attempts check
            if attempts >= config.max_attempts:
                await self.cancel_order(current_order_id)
                self._log_event(
                    current_order_id,
                    order.status,
                    order.status,
                    f"Reprice cancelled after reaching max attempts ({attempts})",
                )
                break

            # Get latest quotes — use option-specific quote symbol if available
            try:
                quote_sym = order.contract.to_quote_symbol() if hasattr(order.contract, "to_quote_symbol") else order.contract.symbol
                quotes = await self.broker.get_quotes([quote_sym])
                quote = quotes.get(quote_sym)
            except Exception:
                continue

            if not quote or quote.bid is None or quote.ask is None:
                continue

            # Compute new target price with escalation:
            #   attempt 1: halfway between current limit and the touch
            #   attempt 2+: AT the touch (refreshed each cycle, chasing moves)
            #   attempt N+ (cross_touch_after_attempts, exits only): THROUGH
            #     the touch by cross_touch_offset — marketable-plus, bounded.
            # Far-outside-NBBO prices (IBKR Error 202) stay impossible: the
            # cross offset is a few ticks, not an unbounded chase.
            cross = (
                config.cross_touch_after_attempts is not None
                and attempts >= config.cross_touch_after_attempts
            )
            new_price = order.limit_price
            if order.side == OrderSide.BUY:
                natural = quote.ask
                if natural is not None and natural > 0 and natural <= config.max_acceptable_buy_price:
                    if cross:
                        target = natural + config.cross_touch_offset
                        new_price = min(target, config.max_acceptable_buy_price)
                    else:
                        target = (order.limit_price + natural) / 2.0 if attempts == 0 else natural
                        new_price = min(target, natural)  # don't exceed ask
            else:
                natural = quote.bid
                if natural is not None and natural > 0 and natural >= config.min_acceptable_sell_price:
                    if cross:
                        target = natural - config.cross_touch_offset
                        new_price = max(target, config.min_acceptable_sell_price)
                    else:
                        target = (order.limit_price + natural) / 2.0 if attempts == 0 else natural
                        new_price = max(target, natural)  # don't go below bid

            # Conform to IBKR minimum price variation BEFORE storing or
            # sending, so internal limit == broker limit (reconciliation).
            if new_price < 3.00:
                new_price = round(new_price, 2)
            else:
                new_price = round(new_price * 20.0) / 20.0

            # Price difference threshold (at least 1 penny)
            if abs(new_price - order.limit_price) < 0.01:
                continue

            # Calculate remaining quantity
            remaining_qty = order.quantity - order.filled_quantity
            if remaining_qty <= 0:
                break

            # Optional fast path: modify the working order's price IN PLACE.
            # Disabled by default (use_in_place_modify=False) because IBKR
            # rejects revisions on 0DTE options and the resulting cancel
            # trips the fail-closed lock. Cancel/replace below is the safe
            # default.
            if config.use_in_place_modify and order.broker_order_id:
                try:
                    modified = await self.broker.modify_order(
                        order.broker_order_id, new_price
                    )
                except Exception as e:
                    modified = False
                    self._log_event(
                        current_order_id,
                        order.status,
                        order.status,
                        f"In-place modify failed ({e}); falling back to cancel/replace",
                    )
                if modified:
                    attempts += 1
                    old_price = order.limit_price
                    order.limit_price = new_price
                    order.updated_at = datetime.now(UTC)
                    self._log_event(
                        current_order_id,
                        order.status,
                        order.status,
                        f"Repriced in place: {old_price} -> {new_price}, attempt={attempts}",
                    )
                    continue

            if order.metadata.get("hard_cancel"):
                break

            # Fallback path: cancel old order, submit replacement (not a hard cancel)
            cancel_success = await self.cancel_order(current_order_id, hard_cancel=False)
            if not cancel_success:
                continue

            # Wait for cancellation status confirmation. Bounded by the
            # order's overall timeout rather than a fixed window: cancel
            # acks took 30s+ live at the RTH open, and giving up on a slow
            # ack abandoned the chain — no replacement was ever submitted
            # and the entry died silently (observed live 2026-06-12).
            while order.status == OrderStatus.CANCEL_PENDING:
                if (datetime.now(UTC) - start_time).total_seconds() >= config.timeout_seconds:
                    break
                await asyncio.sleep(0.1)

            if order.status != OrderStatus.CANCELLED or order.metadata.get("hard_cancel"):
                break

            # Submit replacement order
            attempts += 1
            # Recalculate remaining quantity to prevent race conditions from fills received during cancellation
            remaining_qty = order.quantity - order.filled_quantity
            if remaining_qty <= 0:
                self._log_event(
                    current_order_id,
                    order.status,
                    order.status,
                    f"Reprice replacement skipped because order was fully filled during cancellation ({order.filled_quantity}/{order.quantity})",
                )
                break
            new_plan = OrderPlan(
                order_intent_id=order.order_plan_id,
                position_id=order.position_id,
                is_entry=order.is_entry,
                strategy_id=order.strategy_id,
                contract=order.contract,
                side=order.side,
                quantity=remaining_qty,
                order_type="LMT",
                limit_price=new_price,
                order_ref=order.order_ref,
                algo=order.metadata.get("algo"),
                timestamp=datetime.now(UTC),
            )

            new_order_state = OrderState(
                order_plan_id=new_plan.order_plan_id,
                position_id=new_plan.position_id,
                is_entry=new_plan.is_entry,
                strategy_id=new_plan.strategy_id,
                contract=new_plan.contract,
                side=new_plan.side,
                quantity=new_plan.quantity,
                filled_quantity=0,
                limit_price=new_plan.limit_price,
                # Carry the ORIGINAL submit limit through replacements so
                # slippage is always measured from the first price.
                first_limit_price=order.first_limit_price or order.limit_price,
                order_ref=order.order_ref,
                status=OrderStatus.NEW,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            # Carry batch + algo tags through the replacement chain
            if order.metadata.get("entry_batch"):
                new_order_state.metadata["entry_batch"] = order.metadata["entry_batch"]
            if order.metadata.get("algo"):
                new_order_state.metadata["algo"] = order.metadata["algo"]
            self.orders[new_order_state.order_id] = new_order_state

            # Replacement-chain link: the cancelled order is SUPERSEDED, not
            # failed. Multi-leg coordination must follow this link instead of
            # treating the cancel as a dead leg (live incident 2026-06-11:
            # routine reprice cancels were nuking straddle peer legs).
            order.superseded_by = new_order_state.order_id

            try:
                broker_order = await self.broker.place_order(new_plan)
                new_order_state.broker_order_id = broker_order.broker_order_id
                new_order_state.status = broker_order.status
                new_order_state.updated_at = datetime.now(UTC)

                # Set new active tracking target
                current_order_id = new_order_state.order_id

                self._log_event(
                    new_order_state.order_id,
                    OrderStatus.NEW,
                    new_order_state.status,
                    f"Reprice replacement submitted: price={new_price}, attempt={attempts}",
                )
            except Exception as e:
                new_order_state.status = OrderStatus.ERROR
                new_order_state.error_message = str(e)
                new_order_state.updated_at = datetime.now(UTC)
                self._log_event(
                    new_order_state.order_id,
                    OrderStatus.NEW,
                    OrderStatus.ERROR,
                    f"Reprice replacement failed: {e}",
                )
                break

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _find_order_by_broker_id(self, broker_order_id: Optional[str]) -> Optional[OrderState]:
        if not broker_order_id:
            return None
        for order in self.orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    def _log_event(
        self,
        order_id: UUID,
        prev_status: Optional[OrderStatus],
        new_status: OrderStatus,
        message: str,
    ) -> None:
        event = OrderEvent(
            order_id=order_id,
            previous_status=prev_status,
            new_status=new_status,
            message=message,
            timestamp=datetime.now(UTC),
        )
        self.event_store.log_callback("order_state_transition", event)

        # Log the full OrderState to the event store for state reconstruction
        order = self.orders.get(order_id)
        if order:
            self.event_store.log_callback("order_state_updated", order)
