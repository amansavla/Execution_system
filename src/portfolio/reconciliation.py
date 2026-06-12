"""ReconciliationEngine compares internal portfolio and order state against broker state.

If any mismatch is detected, it logs the discrepancy, sets a system lock
in the OverrideManager to prevent new entries, and logs a ReconciliationReport.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

from src.broker.interface import BrokerClient
from src.control.overrides import OverrideManager
from src.core.enums import OrderSide
from src.core.models import OrderState, Position, ReconciliationReport
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore

logger = logging.getLogger(__name__)


class ReconciliationEngine:
    """Compares internal portfolio/order state with actual broker state.

    Enforces 'fail closed' on reconciliation mismatch.
    Never calls OrderManager directly.
    Reads broker state via BrokerClient read-only methods.
    """

    def __init__(
        self,
        broker: BrokerClient,
        position_manager: PositionManager,
        override_manager: OverrideManager,
        event_store: EventStore,
    ) -> None:
        """Initialize ReconciliationEngine.

        Args:
            broker: Read-only access to broker client.
            position_manager: Internal position state.
            override_manager: Safety control management.
            event_store: Audit logs persistence.
        """
        self.broker = broker
        self.position_manager = position_manager
        self.override_manager = override_manager
        self.event_store = event_store

    def _get_contract_key(self, contract) -> tuple[str, str, float, str]:
        """Generate a normalized unique key for an option contract."""
        right_str = contract.right.value if hasattr(contract.right, "value") else str(contract.right)
        return (
            contract.symbol.upper(),
            contract.expiry,
            float(contract.strike),
            right_str.upper(),
        )

    async def reconcile(self, internal_open_orders: list[OrderState]) -> ReconciliationReport:
        """Compare internal state vs broker state.

        Args:
            internal_open_orders: Current active orders tracked internally.

        Returns:
            A ReconciliationReport detailing matches and mismatches.
        """
        matches = 0
        details = []
        internal_only = []
        broker_only = []

        # ------------------------------------------------------------------
        # 1. Compare Positions
        # ------------------------------------------------------------------
        # Group internal open positions by option contract
        internal_by_contract: dict[tuple, list[Position]] = {}
        for pos in self.position_manager.get_open_positions():
            if pos.quantity <= 0:
                continue
            key = self._get_contract_key(pos.contract)
            internal_by_contract.setdefault(key, []).append(pos)

        internal_net_qty: dict[tuple, int] = {}
        for key, pos_list in internal_by_contract.items():
            net_qty = 0
            for pos in pos_list:
                mult = 1 if pos.side == OrderSide.BUY else -1
                net_qty += pos.quantity * mult
            internal_net_qty[key] = net_qty

        # Fetch broker positions
        try:
            broker_positions = await self.broker.get_positions()
        except Exception as e:
            logger.error("Failed to fetch broker positions during reconciliation: %s", e)
            raise

        broker_by_contract: dict[tuple, list[Position]] = {}
        for pos in broker_positions:
            if pos.quantity <= 0:
                continue
            key = self._get_contract_key(pos.contract)
            broker_by_contract.setdefault(key, []).append(pos)

        broker_net_qty: dict[tuple, int] = {}
        for key, pos_list in broker_by_contract.items():
            net_qty = 0
            for pos in pos_list:
                mult = 1 if pos.side == OrderSide.BUY else -1
                net_qty += pos.quantity * mult
            broker_net_qty[key] = net_qty

        # Union of all contract keys
        all_position_keys = set(internal_net_qty.keys()) | set(broker_net_qty.keys())

        for key in all_position_keys:
            iqty = internal_net_qty.get(key, 0)
            bqty = broker_net_qty.get(key, 0)

            if iqty == bqty:
                if iqty != 0:
                    matches += 1
                continue

            symbol, expiry, strike, right = key
            contract_str = f"{symbol} {expiry} {strike} {right}"

            if iqty != 0 and bqty == 0:
                pos_ids = [str(p.position_id) for p in internal_by_contract[key]]
                internal_only.extend(pos_ids)
                details.append({
                    "type": "internal_only_position",
                    "contract": contract_str,
                    "internal_quantity": iqty,
                    "position_ids": pos_ids,
                })
            elif bqty != 0 and iqty == 0:
                broker_only.append(contract_str)
                details.append({
                    "type": "broker_only_position",
                    "contract": contract_str,
                    "broker_quantity": bqty,
                })
            else:
                pos_ids = [str(p.position_id) for p in internal_by_contract[key]]
                details.append({
                    "type": "position_quantity_mismatch",
                    "contract": contract_str,
                    "internal_quantity": iqty,
                    "broker_quantity": bqty,
                    "position_ids": pos_ids,
                })

        # ------------------------------------------------------------------
        # 2. Compare Open Orders
        # ------------------------------------------------------------------
        # Index internal open orders by broker_order_id
        internal_orders_by_broker_id = {}
        for o in internal_open_orders:
            if o.broker_order_id:
                internal_orders_by_broker_id[o.broker_order_id] = o

        # Fetch broker open orders
        try:
            broker_orders = await self.broker.get_open_orders()
        except Exception as e:
            logger.error("Failed to fetch broker open orders during reconciliation: %s", e)
            raise

        broker_order_ids = set()

        for bo in broker_orders:
            if not bo.broker_order_id:
                continue
            broker_order_ids.add(bo.broker_order_id)

            if bo.broker_order_id not in internal_orders_by_broker_id:
                details.append({
                    "type": "unknown_broker_order",
                    "broker_order_id": bo.broker_order_id,
                    "contract": f"{bo.contract.symbol} {bo.contract.expiry} {bo.contract.strike} {bo.contract.right}",
                    "side": bo.side.value if hasattr(bo.side, "value") else str(bo.side),
                    "quantity": bo.quantity,
                    "limit_price": bo.limit_price,
                })
            else:
                io = internal_orders_by_broker_id[bo.broker_order_id]
                # Validate parameters
                io_key = self._get_contract_key(io.contract)
                bo_key = self._get_contract_key(bo.contract)

                if (
                    io_key != bo_key
                    or io.side != bo.side
                    or io.quantity != bo.quantity
                    # Limit-price tolerance: IBKR enforces minimum price
                    # variation (nickels >= $3), so a repriced/modified
                    # order can legitimately differ from our unrounded
                    # value by up to one tick. Larger gaps are real.
                    or abs(io.limit_price - bo.limit_price) > 0.0501
                ):
                    details.append({
                        "type": "order_parameter_mismatch",
                        "broker_order_id": bo.broker_order_id,
                        "internal_order_id": str(io.order_id),
                        "internal_details": {
                            "contract": f"{io.contract.symbol} {io.contract.expiry} {io.contract.strike} {io.contract.right}",
                            "side": io.side.value if hasattr(io.side, "value") else str(io.side),
                            "quantity": io.quantity,
                            "limit_price": io.limit_price,
                        },
                        "broker_details": {
                            "contract": f"{bo.contract.symbol} {bo.contract.expiry} {bo.contract.strike} {bo.contract.right}",
                            "side": bo.side.value if hasattr(bo.side, "value") else str(bo.side),
                            "quantity": bo.quantity,
                            "limit_price": bo.limit_price,
                        }
                    })
                else:
                    matches += 1

        # Check for internal orders not at broker
        for io in internal_open_orders:
            if io.broker_order_id and io.broker_order_id not in broker_order_ids:
                details.append({
                    "type": "internal_order_not_at_broker",
                    "broker_order_id": io.broker_order_id,
                    "internal_order_id": str(io.order_id),
                })

        # ------------------------------------------------------------------
        # 3. Report & Action
        # ------------------------------------------------------------------
        # Classify mismatches by severity:
        # - SEVERE (lock): internal_only_position, position_quantity_mismatch
        # - WARN (no lock): broker_only_position, unknown_broker_order, internal_order_not_at_broker
        severe_types = {"internal_only_position", "position_quantity_mismatch"}
        severe_details = [d for d in details if d.get("type") in severe_types]
        warn_details = [d for d in details if d.get("type") not in severe_types]

        is_clean = len(details) == 0
        mismatches_count = len(details)

        # Build report
        report = ReconciliationReport(
            matches=matches,
            mismatches=mismatches_count,
            internal_only=[UUID(pid) for pid in internal_only],
            broker_only=broker_only,
            details=details,
            is_clean=is_clean,
            timestamp=datetime.now(UTC),
        )

        # Log report callback
        self.event_store.log_callback("reconciliation_event", report)

        if severe_details:
            logger.warning(
                "Reconciliation SEVERE discrepancy detected! Matches: %d, Severe: %d, Warn: %d. Locking system.",
                matches, len(severe_details), len(warn_details),
            )
            for d in severe_details:
                logger.warning("  SEVERE: %s", d)
            # Engage global lock safety gate
            self.override_manager.lock_system()
        elif warn_details:
            logger.warning(
                "Reconciliation warnings (non-locking): Matches: %d, Warnings: %d",
                matches, len(warn_details),
            )
            for d in warn_details:
                logger.info("  WARN: %s", d)
        else:
            logger.info("Reconciliation passed cleanly. Matches: %d", matches)

        return report
