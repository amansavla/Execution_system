"""Execution quality metrics and PnL analytics calculations.

Reads from EventStore events or repository records and performs deterministic,
unit-testable calculations.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from src.core.enums import OrderSide, OrderStatus

logger = logging.getLogger(__name__)


def _normalize_contract(contract_data: Any) -> dict[str, Any]:
    """Helper to extract symbol, expiry, strike, and right from contract payload."""
    if not contract_data:
        return {}

    # If it is a Pydantic model
    if hasattr(contract_data, "model_dump"):
        data = contract_data.model_dump()
    elif isinstance(contract_data, dict):
        data = contract_data
    else:
        # Fallback for string or raw representation
        return {"symbol": str(contract_data)}

    # Map rights if it's enum
    right = data.get("right")
    if right and hasattr(right, "value"):
        right_str = right.value
    else:
        right_str = str(right) if right else ""

    return {
        "symbol": data.get("symbol") or "",
        "expiry": data.get("expiry") or "",
        "strike": float(data.get("strike")) if data.get("strike") is not None else 0.0,
        "right": right_str.upper(),
    }


def normalize_events(events: list[Any]) -> list[dict[str, Any]]:
    """Convert a mix of SQLite EventRecord objects and raw dicts into normalized dicts.

    Sorted by timestamp ascending.
    """
    normalized = []
    for evt in events:
        # Check if it is an EventRecord from repository
        if hasattr(evt, "event_type") and hasattr(evt, "payload"):
            try:
                if isinstance(evt.timestamp, str):
                    # SQLite stores ISO string
                    ts = datetime.fromisoformat(evt.timestamp)
                else:
                    ts = evt.timestamp
            except Exception:
                ts = datetime.now()

            normalized.append({
                "type": evt.event_type,
                "timestamp": ts,
                "strategy_id": evt.strategy_id,
                "data": evt.payload or {},
            })
        elif isinstance(evt, dict):
            ts = evt.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except Exception:
                    ts = datetime.now()
            elif not isinstance(ts, datetime):
                ts = datetime.now()

            # Strategy ID can be top level or inside nested data
            data_payload = evt.get("data") or {}
            strategy_id = (
                evt.get("strategy_id")
                or (data_payload.get("strategy_id") if isinstance(data_payload, dict) else None)
            )

            normalized.append({
                "type": evt.get("type") or "unknown",
                "timestamp": ts,
                "strategy_id": strategy_id,
                "data": data_payload,
            })
    return sorted(normalized, key=lambda x: x["timestamp"])


def compute_execution_quality(normalized_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate slippage, fill rate, cancel rate, time to fill, and rejections.

    All inputs must be normalized events.
    """
    # 1. Map order submissions and state transitions
    # order_id -> submission_timestamp
    order_submissions: dict[str, datetime] = {}
    # order_id -> completed_timestamp
    order_completions: dict[str, datetime] = {}
    # order_id -> latest seen status
    order_statuses: dict[str, str] = {}
    # order_id -> OrderSide
    order_sides: dict[str, OrderSide] = {}
    # order_id -> contract details
    order_contracts: dict[str, dict[str, Any]] = {}
    # order_id -> strategy_id
    order_strategies: dict[str, str] = {}
    # order_id -> limit price
    order_limit_prices: dict[str, float] = {}
    # order_id -> requested quantity
    order_quantities: dict[str, int] = {}
    # order_id -> list of fill prices and quantities
    order_fills: dict[str, list[tuple[float, int]]] = defaultdict(list)

    # 2. Store quotes and signals for mid-price lookup
    # contract_key -> list of (timestamp, mid_price)
    quote_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)

    # Process all events in order to reconstruct state
    for evt in normalized_events:
        evt_type = evt["type"]
        ts = evt["timestamp"]
        data = evt["data"]

        if not isinstance(data, dict):
            continue

        # Check for QuoteSnapshots or signals
        if evt_type == "signal":
            # Check if signal has contract and quote details
            contract_data = data.get("contract")
            quote_data = data.get("quote") or data.get("underlying_quote")
            if contract_data and quote_data:
                c = _normalize_contract(contract_data)
                key = f"{c.get('symbol')}_{c.get('expiry')}_{c.get('strike')}_{c.get('right')}"
                bid = quote_data.get("bid")
                ask = quote_data.get("ask")
                if bid is not None and ask is not None:
                    mid = (float(bid) + float(ask)) / 2.0
                    quote_history[key].append((ts, mid))
        elif evt_type == "quote_snapshot":
            symbol = data.get("symbol")
            bid = data.get("bid")
            ask = data.get("ask")
            if symbol and bid is not None and ask is not None:
                mid = (float(bid) + float(ask)) / 2.0
                quote_history[symbol].append((ts, mid))

        # Check for order status transitions
        if evt_type in ("order_event", "order_callback", "order_state_transition"):
            order_id = data.get("order_id") or data.get("orderId")
            if order_id:
                order_id_str = str(order_id)
                new_status = data.get("new_status") or data.get("status")

                if isinstance(new_status, OrderStatus):
                    status_str = new_status.value
                else:
                    status_str = str(new_status).upper()

                order_statuses[order_id_str] = status_str

                # Capture submission timestamp
                if status_str in ("NEW", "SUBMITTED") and order_id_str not in order_submissions:
                    order_submissions[order_id_str] = ts

                # Capture completion timestamp
                if status_str in ("FILLED", "CANCELLED", "REJECTED", "ERROR") and order_id_str not in order_completions:
                    order_completions[order_id_str] = ts

                # Reconstruct metadata from order fields if present
                if "contract" in data:
                    order_contracts[order_id_str] = _normalize_contract(data["contract"])
                if "side" in data:
                    side_val = data["side"]
                    order_sides[order_id_str] = (
                        side_val if isinstance(side_val, OrderSide)
                        else (OrderSide.BUY if str(side_val).upper() == "BUY" else OrderSide.SELL)
                    )
                if "strategy_id" in data:
                    order_strategies[order_id_str] = str(data["strategy_id"])
                if "limit_price" in data:
                    order_limit_prices[order_id_str] = float(data["limit_price"])
                elif "limitPrice" in data:
                    order_limit_prices[order_id_str] = float(data["limitPrice"])
                if "quantity" in data:
                    order_quantities[order_id_str] = int(data["quantity"])

        # Check for fills
        if evt_type in ("fill_event", "fill_callback", "fill_received"):
            order_id = data.get("order_id") or data.get("orderId")
            if order_id:
                order_id_str = str(order_id)
                price = data.get("fill_price") or data.get("price")
                qty = data.get("filled_quantity") or data.get("shares")
                if price is not None and qty is not None:
                    order_fills[order_id_str].append((float(price), int(qty)))

                if "side" in data and order_id_str not in order_sides:
                    side_val = data["side"]
                    order_sides[order_id_str] = (
                        side_val if isinstance(side_val, OrderSide)
                        else (OrderSide.BUY if str(side_val).upper() == "BUY" else OrderSide.SELL)
                    )
                if "strategy_id" in data and order_id_str not in order_strategies:
                    order_strategies[order_id_str] = str(data["strategy_id"])
                if "contract" in data and order_id_str not in order_contracts:
                    order_contracts[order_id_str] = _normalize_contract(data["contract"])

    # 3. Calculate Slippage & Metrics
    total_slippage_cost = 0.0
    slippage_counts = 0
    slippage_sum = 0.0

    # Grouped metrics
    strategy_cost: dict[str, float] = defaultdict(float)
    underlying_cost: dict[str, float] = defaultdict(float)

    for order_id_str, fills in order_fills.items():
        side = order_sides.get(order_id_str, OrderSide.BUY)
        contract = order_contracts.get(order_id_str, {})
        strat = order_strategies.get(order_id_str, "unknown")
        symbol = contract.get("symbol", "unknown")

        sub_ts = order_submissions.get(order_id_str)

        # Look up arrival mid price
        contract_key = f"{contract.get('symbol')}_{contract.get('expiry')}_{c.get('strike') if (c := contract) else 0.0}_{contract.get('right')}"
        mid_price = None

        # Try contract key first, then fall back to symbol (underlying)
        for key in (contract_key, symbol):
            if key in quote_history:
                # Find closest quote before submission timestamp
                target_ts = sub_ts or datetime.now()
                best_mid = None
                for q_ts, q_mid in quote_history[key]:
                    if q_ts <= target_ts:
                        best_mid = q_mid
                    else:
                        break
                if best_mid is not None:
                    mid_price = best_mid
                    break

        # Calculate slippage for each fill of this order
        for fill_price, fill_qty in fills:
            if mid_price is not None:
                # Signed slippage: BUY is executed price - mid. SELL is mid - executed price.
                # Standard convention: slippage = (fill - mid) for BUY, (mid - fill) for SELL.
                sign = 1.0 if side == OrderSide.BUY else -1.0
                slippage = (fill_price - mid_price) * sign
                cost = slippage * fill_qty

                total_slippage_cost += cost
                slippage_sum += slippage
                slippage_counts += 1

                strategy_cost[strat] += cost
                underlying_cost[symbol] += cost

    # Calculate Time to Fill
    time_to_fills = []
    for order_id_str, comp_ts in order_completions.items():
        if order_statuses.get(order_id_str) == "FILLED":
            sub_ts = order_submissions.get(order_id_str)
            if sub_ts:
                duration = (comp_ts - sub_ts).total_seconds()
                time_to_fills.append(duration)

    avg_time_to_fill = sum(time_to_fills) / len(time_to_fills) if time_to_fills else 0.0

    # Calculate Rates
    total_orders = len(order_statuses)
    filled_orders = sum(1 for status in order_statuses.values() if status == "FILLED")
    cancelled_orders = sum(1 for status in order_statuses.values() if status == "CANCELLED")
    rejected_orders = sum(1 for status in order_statuses.values() if status in ("REJECTED", "ERROR"))

    # Partial fill rate: orders with fills but not filled status (or cancelled with some fills)
    partial_filled = 0
    for order_id_str, status in order_statuses.items():
        if status != "FILLED" and order_id_str in order_fills:
            partial_filled += 1

    fill_rate = filled_orders / total_orders if total_orders > 0 else 0.0
    cancel_rate = cancelled_orders / total_orders if total_orders > 0 else 0.0
    partial_fill_rate = partial_filled / total_orders if total_orders > 0 else 0.0

    avg_slippage = slippage_sum / slippage_counts if slippage_counts > 0 else 0.0

    return {
        "total_slippage_cost": total_slippage_cost,
        "avg_slippage": avg_slippage,
        "avg_time_to_fill_seconds": avg_time_to_fill,
        "fill_rate": fill_rate,
        "cancel_rate": cancel_rate,
        "partial_fill_rate": partial_fill_rate,
        "rejection_count": rejected_orders,
        "execution_cost_by_strategy": dict(strategy_cost),
        "execution_cost_by_underlying": dict(underlying_cost),
        "total_orders": total_orders,
    }


def compute_realized_pnl(normalized_events: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Compute realized PnL by strategy and underlying using FIFO matching.

    Returns:
        Dict format: {strategy_id: {underlying_symbol: realized_pnl_value}}
    """
    # inventory: (strategy_id, contract_key) -> list of {"quantity": int, "price": float, "side": OrderSide}
    inventory: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    realized_pnl: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    # Reconstruct fills in order
    for evt in normalized_events:
        evt_type = evt["type"]
        data = evt["data"]

        if evt_type not in ("fill_event", "fill_callback", "fill_received"):
            continue

        if not isinstance(data, dict):
            continue

        strat = data.get("strategy_id") or "unknown"
        price = data.get("fill_price") or data.get("price")
        qty = data.get("filled_quantity") or data.get("shares")
        side_val = data.get("side")

        if price is None or qty is None or side_val is None:
            continue

        price = float(price)
        qty = int(qty)
        side = (
            side_val if isinstance(side_val, OrderSide)
            else (OrderSide.BUY if str(side_val).upper() == "BUY" else OrderSide.SELL)
        )

        contract_data = data.get("contract")
        c = _normalize_contract(contract_data)
        symbol = c.get("symbol", "unknown")
        contract_key = f"{symbol}_{c.get('expiry')}_{c.get('strike')}_{c.get('right')}"

        inv_key = (strat, contract_key)
        lots = inventory[inv_key]

        pnl = 0.0
        remaining_qty = qty

        while remaining_qty > 0 and lots:
            first_lot = lots[0]
            # If opposite side, we match
            if first_lot["side"] != side:
                match_qty = min(remaining_qty, first_lot["quantity"])

                # Realized PnL = (Sell Price - Buy Price) * quantity
                if side == OrderSide.SELL:
                    pnl += (price - first_lot["price"]) * match_qty
                else:
                    pnl += (first_lot["price"] - price) * match_qty

                # Reduce inventory
                first_lot["quantity"] -= match_qty
                remaining_qty -= match_qty

                if first_lot["quantity"] == 0:
                    lots.pop(0)
            else:
                # Same side, cannot match opposing sides
                break

        # If there is remaining quantity, it forms a new inventory lot
        if remaining_qty > 0:
            lots.append({
                "quantity": remaining_qty,
                "price": price,
                "side": side,
            })

        if pnl != 0.0:
            realized_pnl[strat][symbol] += pnl

    return {strat: dict(underlying_pnl) for strat, underlying_pnl in realized_pnl.items()}
