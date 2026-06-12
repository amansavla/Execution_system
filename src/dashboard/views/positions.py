"""Positions view panel for the Operator Dashboard."""

from __future__ import annotations

from uuid import UUID
import streamlit as st
import pandas as pd

from src.control.manual_control import ManualControlService


def render_positions(manual_control: ManualControlService) -> None:
    """Render the active positions list and manual flattening controls."""
    st.header("💼 Open Positions")

    try:
        positions_raw = manual_control.show_positions()
    except Exception as e:
        st.error(f"Failed to load open positions: {e}")
        return

    if not positions_raw:
        st.info("No open positions tracked in the system.")
        return

    df = pd.DataFrame(positions_raw)
    st.dataframe(df, use_container_width=True)

    st.markdown("---")
    st.subheader("🔧 Manual Flatten Position")

    pos_options = {
        f"{pos['contract']} (ID: {pos['position_id'][:8]})": pos["position_id"]
        for pos in positions_raw
    }

    selected_pos_label = st.selectbox("Select Position to Flatten", list(pos_options.keys()))
    if selected_pos_label:
        selected_id = pos_options[selected_pos_label]

        # Find selected position detail
        selected_pos = next((p for p in positions_raw if p["position_id"] == selected_id), None)

        if selected_pos:
            st.warning(
                f"You are about to flatten position: {selected_pos['contract']} "
                f"({selected_pos['side']} {selected_pos['quantity']} contracts, Entry Price: ${selected_pos['entry_price']})"
            )

            exit_price = st.number_input(
                "Manual Exit Price ($)",
                min_value=0.01,
                value=float(selected_pos["entry_price"]),
                step=0.01
            )

            if st.button("Request Flatten Position", key="req_flatten"):
                st.session_state["confirm_flatten_id"] = selected_id

            if st.session_state.get("confirm_flatten_id") == selected_id:
                st.warning("⚠️ Are you sure you want to execute manual flatten on this position?")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Yes, Confirm Flatten", key="exec_flatten", type="primary"):
                        try:
                            res = manual_control.flatten_position(UUID(selected_id), exit_price)
                            st.success(f"Command executed: {res}")
                            st.session_state.pop("confirm_flatten_id", None)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to execute flattening: {e}")
                with c2:
                    if st.button("Cancel", key="cancel_flatten"):
                        st.session_state.pop("confirm_flatten_id", None)
                        st.rerun()
