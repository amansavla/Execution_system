"""Orders view panel for the Operator Dashboard."""

from __future__ import annotations

import sqlite3
import json
from uuid import UUID
import streamlit as st
import pandas as pd
from datetime import datetime

from src.control.manual_control import ManualControlService


def get_order_history(db_path: str = "data/events.db") -> list[dict]:
    """Retrieve and reconstruct the complete order history from SQLite events."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    cursor = conn.cursor()
    try:
        # Fetch order-related events chronologically
        cursor.execute(
            "SELECT event_type, timestamp, strategy_id, payload FROM events "
            "WHERE event_type IN ('order_event', 'order_callback', 'order_state_transition', 'order_state_updated', 'fill_event', 'exit_triggered', 'manual_override') "
            "ORDER BY timestamp ASC"
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    # Track exit triggers per position ID
    position_triggers = {}
    for event_type, ts, strat_id, payload_str in rows:
        if event_type == 'exit_triggered':
            try:
                payload = json.loads(payload_str)
                pos_id = payload.get('position_id')
                trigger = payload.get('trigger')
                if pos_id and trigger:
                    position_triggers[pos_id] = trigger
            except Exception:
                continue

    orders_map = {}
    for event_type, ts, strat_id, payload_str in rows:
        try:
            payload = json.loads(payload_str)
        except Exception:
            continue

        order_id = payload.get('order_id')
        if not order_id and 'order' in payload:
            if isinstance(payload['order'], dict):
                order_id = payload['order'].get('order_id')
        if not order_id and 'metadata' in payload:
            if isinstance(payload['metadata'], dict):
                order_id = payload['metadata'].get('order_id')

        if not order_id:
            continue

        if order_id not in orders_map:
            orders_map[order_id] = {
                'order_id': order_id,
                'strategy_id': strat_id,
                'contract': '',
                'side': '',
                'quantity': 0,
                'filled_quantity': 0,
                'limit_price': 0.0,
                'avg_fill_price': 0.0,
                'status': 'NEW',
                'reason': '',
                'timestamp': ts,
                'last_updated': ts,
                'messages': [],
                'is_entry': True
            }

        o = orders_map[order_id]
        if strat_id and not o['strategy_id']:
            o['strategy_id'] = strat_id

        # Update order details
        if event_type == 'order_state_updated':
            if 'contract' in payload and isinstance(payload['contract'], dict):
                c = payload['contract']
                o['contract'] = f"{c.get('symbol')} {c.get('expiry')} {c.get('strike')} {c.get('right')}"
            o['side'] = payload.get('side', o['side'])
            o['quantity'] = payload.get('quantity', o['quantity'])
            o['filled_quantity'] = payload.get('filled_quantity', o['filled_quantity'])
            o['limit_price'] = payload.get('limit_price', o['limit_price'])
            o['status'] = payload.get('status', o['status'])
            o['position_id'] = payload.get('position_id', o.get('position_id'))
            o['is_entry'] = payload.get('is_entry', o.get('is_entry', True))

        elif event_type == 'order_state_transition':
            new_status = payload.get('new_status')
            msg = payload.get('message', '')
            if new_status:
                o['status'] = new_status
            o['last_updated'] = ts
            if msg:
                o['messages'].append(msg)

            if 'Reprice replacement' in msg:
                o['reason'] = 'Repricer'
            elif 'Reprice timed out' in msg:
                o['reason'] = 'Reprice Timeout'
            elif 'Reprice cancelled' in msg:
                o['reason'] = 'Reprice Limit'
            elif 'Order cancellation requested' in msg:
                o['reason'] = 'Cancelled'

        elif event_type == 'fill_event':
            o['filled_quantity'] = payload.get('filled_quantity', o['filled_quantity'])
            o['avg_fill_price'] = payload.get('fill_price', o['avg_fill_price'])
            o['status'] = 'FILLED'

        elif event_type == 'order_event':
            new_status = payload.get('new_status')
            msg = payload.get('message', '')
            if new_status:
                o['status'] = new_status
            if msg:
                o['messages'].append(msg)

    # Post-process trigger reasons and filter records
    history_list = []
    for order_id, o in orders_map.items():
        if not o['side']:
            continue  # skip skeletons

        # Format contract
        if not o['contract'] and 'contract' in o:
            c = o['contract']
            if isinstance(c, dict):
                o['contract'] = f"{c.get('symbol')} {c.get('expiry')} {c.get('strike')} {c.get('right')}"

        # Determine Reason/Trigger
        pos_id = o.get('position_id')
        is_entry = o.get('is_entry', True)

        if pos_id and pos_id in position_triggers:
            o['reason'] = f"Exit: {position_triggers[pos_id].replace('_', ' ').title()}"
        elif not is_entry:
            o['reason'] = "Exit Order"
        else:
            if o['status'] == 'FILLED':
                o['reason'] = "Strategy Entry"
            elif o['status'] == 'CANCELLED':
                if not o['reason']:
                    o['reason'] = 'Manual / System Cancel'
            elif o['status'] in ('REJECTED', 'ERROR'):
                errs = [m for m in o['messages'] if any(x in m.lower() for x in ('fail', 'reject', 'error'))]
                o['reason'] = errs[0] if errs else f"Error ({o['status']})"
            elif not o['reason']:
                o['reason'] = 'Working'

        history_list.append(o)

    # Sort descending
    history_list.sort(key=lambda x: x['timestamp'], reverse=True)
    return history_list


def render_orders(manual_control: ManualControlService) -> None:
    """Render the active orders panel and manual order cancellation controls."""
    st.header("📥 Active Orders")

    try:
        orders_raw = manual_control.show_orders()
    except Exception as e:
        st.error(f"Failed to load active orders: {e}")
        return

    if not orders_raw:
        st.info("No active/open orders in the system.")
    else:
        df = pd.DataFrame(orders_raw)
        st.dataframe(df, use_container_width=True)

        st.markdown("---")
        st.subheader("❌ Cancel Order")

        order_options = {
            f"{order['contract']} ({order['side']} {order['quantity']} @ ${order['limit_price']})": order["order_id"]
            for order in orders_raw
        }

        selected_order_label = st.selectbox("Select Order to Cancel", list(order_options.keys()))
        if selected_order_label:
            selected_id = order_options[selected_order_label]

            if st.button("Cancel Selected Order"):
                try:
                    import asyncio
                    res = asyncio.run(manual_control.cancel_order(UUID(selected_id)))
                    st.success(f"Command executed: {res}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to cancel order: {e}")

    # Cancel All Orders Panel (Dangerous Action)
    st.markdown("---")
    st.subheader("🚨 Emergency: Cancel All Orders")
    st.write("Cancel all active/working orders across the entire system.")

    if st.button("Request Cancel All Orders", key="req_cancel_all", type="primary"):
        st.session_state["confirm_cancel_all"] = True

    if st.session_state.get("confirm_cancel_all"):
        st.warning("⚠️ Are you absolutely sure you want to cancel ALL active orders across the system?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Yes, Confirm Cancel All", key="exec_cancel_all", type="primary"):
                try:
                    import asyncio
                    res = asyncio.run(manual_control.cancel_all())
                    st.success(f"Command executed: {res}")
                    st.session_state.pop("confirm_cancel_all", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to execute cancel all: {e}")
        with c2:
            if st.button("Cancel", key="abort_cancel_all"):
                st.session_state.pop("confirm_cancel_all", None)
                st.rerun()

    # --- ORDER HISTORY SECTION ---
    st.markdown("---")
    st.header("📜 Order History")

    history = get_order_history()
    if not history:
        st.info("No order history found in events database.")
    else:
        # Get unique strategies for filtering
        strategies = sorted(list({o['strategy_id'] for o in history if o['strategy_id']}))
        selected_strat = st.selectbox("Filter by Strategy", ["All"] + strategies)

        # Filter list
        filtered_history = history
        if selected_strat != "All":
            filtered_history = [o for o in history if o['strategy_id'] == selected_strat]

        # Display Metrics
        total_count = len(filtered_history)
        filled_count = sum(1 for o in filtered_history if o['status'] == 'FILLED')
        cancelled_count = sum(1 for o in filtered_history if o['status'] == 'CANCELLED')
        failed_count = sum(1 for o in filtered_history if o['status'] in ('REJECTED', 'ERROR'))

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Orders", total_count)
        m2.metric("Filled", filled_count)
        m3.metric("Cancelled", cancelled_count)
        m4.metric("Failed/Error", failed_count)

        # Build clean DataFrame
        df_data = []
        for o in filtered_history:
            # Parse timestamp to clean format
            try:
                dt = datetime.fromisoformat(o['timestamp'].replace('Z', '+00:00'))
                time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                time_str = o['timestamp']

            qty_str = f"{o['filled_quantity']} / {o['quantity']}"

            df_data.append({
                "Timestamp": time_str,
                "Strategy": o['strategy_id'] or "Manual/Global",
                "Contract": o['contract'] or "Unknown",
                "Side": o['side'],
                "Qty (Filled/Total)": qty_str,
                "Limit Price": f"${o['limit_price']:.2f}" if o['limit_price'] else "-",
                "Avg Fill Price": f"${o['avg_fill_price']:.2f}" if o['avg_fill_price'] else "-",
                "Status": o['status'],
                "Reason / Trigger": o['reason']
            })

        history_df = pd.DataFrame(df_data)
        st.dataframe(history_df, use_container_width=True, hide_index=True)
