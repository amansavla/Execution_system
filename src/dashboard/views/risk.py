"""Risk controls panel for the Operator Dashboard."""

from __future__ import annotations

import streamlit as st
from src.control.manual_control import ManualControlService


def render_risk(manual_control: ManualControlService) -> None:
    """Render system locks, symbol disables, and risk override controls."""
    st.header("🛡️ Risk Overrides")

    try:
        status_info = manual_control.status()
    except Exception as e:
        st.error(f"Failed to fetch risk status: {e}")
        return

    # Render Lock System Action (Dangerous)
    st.subheader("🔒 Emergency System Lock")
    st.write(
        "Locking the system immediately blocks all new entry/exit orders system-wide. "
        "Use this for critical infrastructure or broker issues."
    )

    if status_info.get("system_locked"):
        st.error("🚨 SYSTEM IS CURRENTLY LOCKED!")
        if st.button("Request Unlock System", key="req_unlock", type="primary"):
            st.session_state["confirm_unlock"] = True

        if st.session_state.get("confirm_unlock"):
            st.warning("⚠️ Are you sure you want to unlock the system?")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, Confirm Unlock", key="exec_unlock", type="primary"):
                    try:
                        manual_control.override_manager.unlock_system()
                        st.success("System successfully unlocked.")
                        st.session_state.pop("confirm_unlock", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to unlock: {e}")
            with c2:
                if st.button("Cancel", key="abort_unlock"):
                    st.session_state.pop("confirm_unlock", None)
                    st.rerun()
    else:
        st.success("System is currently unlocked and trading.")
        if st.button("Request Lock System", key="req_lock", type="primary"):
            st.session_state["confirm_lock"] = True

        if st.session_state.get("confirm_lock"):
            st.warning("⚠️ Are you sure you want to Lock the system? This blocks all trading activity.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, Confirm Lock", key="exec_lock", type="primary"):
                    try:
                        res = manual_control.lock_system()
                        st.success(f"System locked: {res}")
                        st.session_state.pop("confirm_lock", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to lock: {e}")
            with c2:
                if st.button("Cancel", key="abort_lock"):
                    st.session_state.pop("confirm_lock", None)
                    st.rerun()

    st.markdown("---")

    # Global Reduce Only Toggles
    st.subheader("⚠️ Global Reduce-Only Mode")
    st.write("Enable reduce-only mode globally to prevent any strategy from placing entry orders.")

    if status_info.get("reduce_only"):
        st.warning("Global Reduce-Only mode is ACTIVE.")
        if st.button("Disable Global Reduce-Only"):
            try:
                manual_control.override_manager.set_reduce_only(False)
                st.success("Global Reduce-Only mode disabled.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to disable: {e}")
    else:
        st.info("Global Reduce-Only mode is inactive.")
        if st.button("Request Global Reduce-Only", key="req_ro"):
            st.session_state["confirm_ro"] = True

        if st.session_state.get("confirm_ro"):
            st.warning("⚠️ Are you sure you want to enable global reduce-only?")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, Confirm Reduce-Only", key="exec_ro", type="primary"):
                    try:
                        res = manual_control.reduce_only()
                        st.success(f"Reduce-Only enabled: {res}")
                        st.session_state.pop("confirm_ro", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to enable: {e}")
            with c2:
                if st.button("Cancel", key="abort_ro"):
                    st.session_state.pop("confirm_ro", None)
                    st.rerun()

    st.markdown("---")

    # Symbol Enable / Disable
    st.subheader("🚫 Disable Underlying Symbol")
    st.write("Prevent the system from entering positions on specific underlying tickers.")

    disabled_symbols = status_info.get("disabled_symbols", [])
    if disabled_symbols:
        st.write("Currently Disabled Symbols:")
        for sym in disabled_symbols:
            col1, col2 = st.columns([3, 1])
            with col1:
                st.code(sym)
            with col2:
                if st.button("Re-enable", key=f"enable_{sym}"):
                    try:
                        manual_control.enable_symbol(sym)
                        st.success(f"Enabled {sym}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed: {e}")
    else:
        st.success("No symbols are currently disabled.")

    st.write("**Disable a New Symbol:**")
    new_sym = st.text_input("Enter symbol (e.g. SPY, AAPL, SPX)", "").strip().upper()
    if st.button("Disable Symbol", disabled=not new_sym):
        try:
            res = manual_control.disable_symbol(new_sym)
            st.success(f"Command executed: {res}")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to disable symbol: {e}")
