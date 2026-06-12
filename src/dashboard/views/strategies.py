"""Strategies status panel for the Operator Dashboard."""

from __future__ import annotations

from pathlib import Path
import streamlit as st
import pandas as pd

from src.core.config import load_strategies_config
from src.control.manual_control import ManualControlService


def render_strategies(manual_control: ManualControlService) -> None:
    """Render strategy states and pause/resume buttons."""
    st.header("📈 Strategy Controls")

    # Load strategy IDs from:
    # 1. Config file
    # 2. Status overrides
    # 3. Open positions & active orders
    strategy_ids: set[str] = set()

    try:
        config_path = Path("configs/strategies.yaml")
        if config_path.exists():
            cfg = load_strategies_config(config_path)
            for s in cfg.strategies:
                strategy_ids.add(s.strategy_id)
    except Exception:
        pass

    try:
        status_info = manual_control.status()
        strategy_ids.update(status_info.get("paused_strategies", []))
        strategy_ids.update(status_info.get("reduce_only_strategies", []))
    except Exception:
        pass

    try:
        positions = manual_control.show_positions()
        for p in positions:
            strategy_ids.add(p["strategy_id"])
    except Exception:
        pass

    # Ensure a default is present
    if not strategy_ids:
        strategy_ids.add("example_put_spread")

    st.subheader("Configured / Active Strategies")
    
    # Render table of current strategy states
    rows = []
    for s_id in sorted(strategy_ids):
        status_info = manual_control.status()
        is_paused = s_id in status_info.get("paused_strategies", [])
        is_ro = s_id in status_info.get("reduce_only_strategies", [])
        
        state_label = "🔴 PAUSED" if is_paused else ("⚠️ REDUCE-ONLY" if is_ro else "🟢 ACTIVE")
        rows.append({
            "Strategy ID": s_id,
            "State": state_label
        })

    st.table(pd.DataFrame(rows))

    st.markdown("---")
    st.subheader("🔧 Modify Strategy State")

    selected_strat = st.selectbox("Select Strategy", sorted(strategy_ids))

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("▶️ Resume / Run", use_container_width=True):
            try:
                # Remove pause
                manual_control.resume_strategy(selected_strat)
                # Remove reduce-only if applied
                manual_control.override_manager.set_reduce_only(False, selected_strat)
                st.success(f"Strategy {selected_strat} is now running.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    with c2:
        if st.button("⏸️ Pause Strategy", use_container_width=True):
            try:
                manual_control.pause_strategy(selected_strat)
                st.success(f"Strategy {selected_strat} is now paused.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    with c3:
        if st.button("⚠️ Set Reduce-Only", use_container_width=True):
            try:
                manual_control.reduce_only(selected_strat)
                st.success(f"Strategy {selected_strat} set to Reduce-Only.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")
