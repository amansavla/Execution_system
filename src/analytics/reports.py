"""Execution quality and PnL reporting generators.

Formats raw analytics dictionaries into user-friendly text reports or summaries.
"""

from __future__ import annotations

from typing import Any


def generate_text_report(quality_metrics: dict[str, Any], realized_pnl: dict[str, dict[str, float]]) -> str:
    """Generate a formatted plain text report of execution metrics and realized PnL."""
    lines = []
    lines.append("=" * 60)
    lines.append("            SYSTEM EXECUTION QUALITY & PNL REPORT")
    lines.append("=" * 60)

    # General Stats
    lines.append(f"Total Orders Submitted : {quality_metrics.get('total_orders', 0)}")
    lines.append(f"Fill Rate              : {quality_metrics.get('fill_rate', 0.0) * 100:.2f}%")
    lines.append(f"Cancel Rate            : {quality_metrics.get('cancel_rate', 0.0) * 100:.2f}%")
    lines.append(f"Partial Fill Rate      : {quality_metrics.get('partial_fill_rate', 0.0) * 100:.2f}%")
    lines.append(f"Rejection Count        : {quality_metrics.get('rejection_count', 0)}")
    lines.append(f"Avg Time to Fill       : {quality_metrics.get('avg_time_to_fill_seconds', 0.0):.2f}s")
    lines.append(f"Avg Slippage           : ${quality_metrics.get('avg_slippage', 0.0):.4f}")
    lines.append(f"Total Slippage Cost    : ${quality_metrics.get('total_slippage_cost', 0.0):.2f}")
    lines.append("")

    # Cost Breakdown by Strategy
    lines.append("-" * 60)
    lines.append(" Slippage Cost by Strategy")
    lines.append("-" * 60)
    strat_costs = quality_metrics.get("execution_cost_by_strategy", {})
    if strat_costs:
        for strat, cost in strat_costs.items():
            lines.append(f" - {strat:<30} : ${cost:.2f}")
    else:
        lines.append(" (No slippage costs recorded)")
    lines.append("")

    # Cost Breakdown by Underlying
    lines.append("-" * 60)
    lines.append(" Slippage Cost by Underlying Symbol")
    lines.append("-" * 60)
    sym_costs = quality_metrics.get("execution_cost_by_underlying", {})
    if sym_costs:
        for sym, cost in sym_costs.items():
            lines.append(f" - {sym:<30} : ${cost:.2f}")
    else:
        lines.append(" (No slippage costs recorded)")
    lines.append("")

    # Realized PnL Breakdown
    lines.append("=" * 60)
    lines.append(" Realized PnL (FIFO)")
    lines.append("=" * 60)
    if realized_pnl:
        total_pnl = 0.0
        for strat, underlyings in realized_pnl.items():
            lines.append(f" Strategy: {strat}")
            for symbol, pnl in underlyings.items():
                lines.append(f"   - {symbol:<28} : ${pnl:.2f}")
                total_pnl += pnl
        lines.append("-" * 60)
        lines.append(f" Total Realized PnL            : ${total_pnl:.2f}")
    else:
        lines.append(" (No realized PnL recorded)")

    lines.append("=" * 60)
    return "\n".join(lines)
