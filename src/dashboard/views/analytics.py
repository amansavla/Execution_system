"""Analytics view panel for the Operator Dashboard."""

from __future__ import annotations

import streamlit as st
import pandas as pd

from src.analytics.execution_quality import (
    compute_execution_quality,
    compute_realized_pnl,
    normalize_events,
)
from src.control.manual_control import ManualControlService


def render_analytics(manual_control: ManualControlService) -> None:
    """Render the execution quality metrics, slippage report, and PnL breakdown."""
    st.header("📊 Execution Quality & PnL Analytics")

    # Load all events from event store
    try:
        import asyncio
        event_store = manual_control.event_store
        
        # In-memory events fallback for tests, query SQLite for disk database
        if event_store._repo:
            events_raw = asyncio.run(event_store._repo.get_all())
        else:
            events_raw = event_store.events

        if not events_raw:
            st.info("No events recorded in EventStore to run analytics.")
            return

        normalized = normalize_events(events_raw)
        metrics = compute_execution_quality(normalized)
        realized_pnl = compute_realized_pnl(normalized)

    except Exception as e:
        st.error(f"Failed to calculate analytics: {e}")
        return

    # Render summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Traded Orders", metrics.get("total_orders", 0))
    with col2:
        st.metric("Fill Rate", f"{metrics.get('fill_rate', 0.0) * 100:.2f}%")
    with col3:
        st.metric("Avg Slippage", f"${metrics.get('avg_slippage', 0.0):.4f}")
    with col4:
        st.metric("Total Slippage Cost", f"${metrics.get('total_slippage_cost', 0.0):.2f}")

    st.markdown("---")

    # Rates summary
    st.subheader("Order Outcome Rates")
    rates_df = pd.DataFrame([
        {"Outcome": "Fill Rate", "Percentage": metrics.get("fill_rate", 0.0) * 100},
        {"Outcome": "Cancel Rate", "Percentage": metrics.get("cancel_rate", 0.0) * 100},
        {"Outcome": "Partial Fill Rate", "Percentage": metrics.get("partial_fill_rate", 0.0) * 100},
    ])
    st.bar_chart(rates_df.set_index("Outcome"), y="Percentage")

    # Cost breakdown tables
    st.markdown("---")
    st.subheader("Slippage Cost Breakdown")

    col_strat, col_und = st.columns(2)

    with col_strat:
        st.write("**By Strategy:**")
        strat_costs = metrics.get("execution_cost_by_strategy", {})
        if strat_costs:
            df_strat = pd.DataFrame(
                [{"Strategy ID": k, "Slippage Cost ($)": v} for k, v in strat_costs.items()]
            )
            st.dataframe(df_strat, use_container_width=True)
        else:
            st.write("No cost data recorded.")

    with col_und:
        st.write("**By Underlying:**")
        und_costs = metrics.get("execution_cost_by_underlying", {})
        if und_costs:
            df_und = pd.DataFrame(
                [{"Underlying Symbol": k, "Slippage Cost ($)": v} for k, v in und_costs.items()]
            )
            st.dataframe(df_und, use_container_width=True)
        else:
            st.write("No cost data recorded.")

    # Realized PnL breakdown
    st.markdown("---")
    st.subheader("💵 Realized PnL (FIFO)")
    
    pnl_rows = []
    if realized_pnl:
        for strat, symbols in realized_pnl.items():
            for symbol, val in symbols.items():
                pnl_rows.append({
                    "Strategy ID": strat,
                    "Underlying Symbol": symbol,
                    "Realized PnL ($)": val
                })
        
        df_pnl = pd.DataFrame(pnl_rows)
        st.dataframe(df_pnl, use_container_width=True)

        # Plot PnL by Strategy
        st.write("**PnL by Strategy Plot:**")
        df_pnl_group = df_pnl.groupby("Strategy ID")["Realized PnL ($)"].sum().reset_index()
        st.bar_chart(df_pnl_group.set_index("Strategy ID"), y="Realized PnL ($)")
    else:
        st.info("No realized PnL recorded.")
