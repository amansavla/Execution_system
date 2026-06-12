"""Status view panel for the Operator Dashboard."""

from __future__ import annotations

import streamlit as st
from src.control.manual_control import ManualControlService


def render_status(manual_control: ManualControlService) -> None:
    """Render overall system status, strategy counters, and toggles."""
    st.header("⚡ System Status Summary")

    try:
        status_info = manual_control.status()
    except Exception as e:
        st.error(f"Failed to fetch system status: {e}")
        return

    # Layout status metrics
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if status_info.get("system_locked"):
            st.metric("System Lock State", "LOCKED 🔒", delta="Trading Disabled", delta_color="inverse")
        else:
            st.metric("System Lock State", "UNLOCKED 🔓", delta="Trading Enabled", delta_color="normal")

    with col2:
        if status_info.get("reduce_only"):
            st.metric("Trading Mode", "REDUCE ONLY ⚠️", delta="No New Entries")
        else:
            st.metric("Trading Mode", "NORMAL FLOW ✅", delta="All Orders Allowed")

    with col3:
        st.metric("Active Positions", status_info.get("open_positions", 0))

    with col4:
        st.metric("Active Orders", status_info.get("active_orders", 0))

    st.markdown("---")

    # Quick Toggles & Overrides List
    st.subheader("Current Active Overrides")
    c1, c2, c3 = st.columns(3)

    with c1:
        st.write("**Paused Strategies:**")
        paused = status_info.get("paused_strategies", [])
        if paused:
            for strat in paused:
                st.warning(f"⏸️ {strat}")
        else:
            st.success("None")

    with c2:
        st.write("**Disabled Symbols:**")
        disabled = status_info.get("disabled_symbols", [])
        if disabled:
            for sym in disabled:
                st.warning(f"🚫 {sym}")
        else:
            st.success("None")

    with c3:
        st.write("**Reduce-Only Strategies:**")
        ro_strats = status_info.get("reduce_only_strategies", [])
        if ro_strats:
            for strat in ro_strats:
                st.warning(f"⚠️ {strat}")
        else:
            st.success("None")
