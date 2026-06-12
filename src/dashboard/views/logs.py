"""Logs view panel for the Operator Dashboard."""

from __future__ import annotations

import streamlit as st
import pandas as pd
from src.control.manual_control import ManualControlService


def render_logs(manual_control: ManualControlService) -> None:
    """Render a query interface and event log list from EventStore."""
    st.header("📋 Event Log Audit Trail")

    event_store = manual_control.event_store

    # Search & Filters
    st.subheader("Filter Events")
    
    event_types = [
        "ALL", "signal", "risk_decision", "order_event", "fill_event",
        "position_update", "exit_decision", "manual_override",
        "reconciliation_event", "error", "order_callback", "fill_callback"
    ]
    selected_type = st.selectbox("Event Type Filter", event_types)

    limit = st.slider("Max Events to Display", min_value=10, max_value=200, value=50, step=10)

    try:
        import asyncio
        if event_store._repo:
            if selected_type == "ALL":
                records = asyncio.run(event_store._repo.get_all())
            else:
                records = asyncio.run(event_store.query_by_type(selected_type))
        else:
            # Fallback to in-memory events
            records_raw = event_store.events
            records = []
            for r in records_raw:
                from src.storage.repositories import EventRecord
                records.append(EventRecord(
                    event_id="mem",
                    event_type=r.get("type", "unknown"),
                    timestamp=r["timestamp"].isoformat() if hasattr(r["timestamp"], "isoformat") else str(r["timestamp"]),
                    strategy_id=r.get("strategy_id") or r.get("data", {}).get("strategy_id"),
                    payload=r.get("data") or {}
                ))

        if not records:
            st.info("No events found matching current filters.")
            return

        # Sort descending (newest first)
        records = sorted(records, key=lambda x: x.timestamp, reverse=True)[:limit]

        # Convert to pandas dataframe
        rows = []
        for r in records:
            rows.append({
                "Timestamp": r.timestamp,
                "Event Type": r.event_type,
                "Strategy ID": r.strategy_id or "-",
                "Payload Details": str(r.payload)[:150] + ("..." if len(str(r.payload)) > 150 else "")
            })

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)

        st.markdown("---")
        st.subheader("Inspect Specific Event")
        event_options = {
            f"[{r.timestamp}] {r.event_type} (ID: {r.event_id[:8]}...)": r
            for r in records
        }
        selected_event_label = st.selectbox("Select event details to view", list(event_options.keys()))
        if selected_event_label:
            selected_record = event_options[selected_event_label]
            st.json(selected_record.payload)

    except Exception as e:
        st.error(f"Failed to fetch event logs: {e}")
