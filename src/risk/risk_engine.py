"""Deterministic RiskEngine for the execution system.

RiskEngine evaluates a StrategySignal against the current system state
and returns a RiskDecision. It is purely deterministic — same inputs
always produce the same output.

Allowed dependencies (per AGENTS.md component boundaries):
  - QuoteCache data (passed in as QuoteSnapshot)
  - Config objects (FullRiskConfig, StrategiesConfig, OverridesConfig)
  - Models and enums from src.core

Forbidden dependencies:
  - BrokerClient or any broker module
  - OrderManager
  - PositionManager (state passed in, not called directly)

The RiskEngine never modifies external state. It receives snapshots
and returns a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Optional
from uuid import UUID

from src.core.config import (
    FullRiskConfig,
    OverridesConfig,
    StrategyConfig,
)
from src.core.enums import (
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)
from src.core.models import (
    QuoteSnapshot,
    RiskDecision,
    StrategySignal,
)
from src.marketdata.data_quality import (
    validate_quote_freshness,
    validate_quote_prices,
)


# ===================================================================
# System state snapshot — passed into RiskEngine per evaluation
# ===================================================================

@dataclass(frozen=True)
class SystemState:
    """Immutable snapshot of system state for a single risk evaluation.

    This is the only way RiskEngine sees the outside world. All data
    is passed in — RiskEngine never queries external systems.
    """

    # Current positions: list of (strategy_id, underlying, status) tuples
    open_positions: list[PositionInfo] = field(default_factory=list)

    # Current open orders count
    open_order_count: int = 0

    # Daily PnL tracking
    daily_pnl: float = 0.0
    strategy_daily_pnl: dict[str, float] = field(default_factory=dict)

    # Current quote for the signal's contract
    quote: Optional[QuoteSnapshot] = None

    # Kill switch state
    kill_switch_engaged: bool = False

    # Current UTC time (injectable for testing)
    current_time: Optional[datetime] = None

    # Cooldown: strategy_id -> last trade completion time
    last_trade_times: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionInfo:
    """Minimal position info needed for risk checks.

    Avoids importing the full Position model to keep the interface
    lightweight and avoid circular dependencies.
    """

    strategy_id: str
    underlying: str
    status: PositionStatus
    quantity: int = 0


# ===================================================================
# RiskEngine
# ===================================================================

class RiskEngine:
    """Deterministic risk evaluator.

    Constructed with risk config, strategy configs, and overrides.
    Evaluates signals against current system state.

    Usage:
        engine = RiskEngine(risk_config, strategy_configs, overrides)
        decision = engine.evaluate(signal, system_state)
    """

    def __init__(
        self,
        risk_config: FullRiskConfig,
        strategy_configs: dict[str, StrategyConfig],
        overrides: OverridesConfig,
    ) -> None:
        self._risk = risk_config
        self._strategies = strategy_configs
        self._overrides = overrides

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------

    def evaluate(
        self,
        signal: StrategySignal,
        state: SystemState,
    ) -> RiskDecision:
        """Evaluate a strategy signal and return a risk decision.

        Runs all checks in order. Collects all blocking reasons and
        warnings (does not short-circuit) so the caller gets a
        complete picture.
        """
        blocking: list[str] = []
        warnings: list[str] = []

        now = state.current_time or datetime.now(UTC)

        # ----- Check order -----
        # 1. Kill switch
        self._check_kill_switch(state, blocking)

        # 2. Global trading mode
        self._check_trading_mode(blocking)

        # 3. System locked
        self._check_system_locked(blocking)

        # 4. Strategy enabled
        self._check_strategy_enabled(signal, blocking)

        # 5. Strategy paused (manual override)
        self._check_strategy_paused(signal, blocking)

        # 6. Symbol disabled
        self._check_symbol_disabled(signal, blocking)

        # 7. Reduce-only mode
        self._check_reduce_only(signal, blocking)

        # 8. No-new-trades cutoff
        self._check_cutoff_time(now, blocking)

        # 9. Daily loss limit (global)
        self._check_daily_loss_limit(state, blocking)

        # 10. Per-strategy daily loss limit
        self._check_strategy_daily_loss_limit(signal, state, blocking)

        # 11. Max contracts per trade
        self._check_max_contracts(signal, blocking, warnings)

        # 12. Max open positions (global)
        self._check_max_open_positions(state, blocking)

        # 13. Max positions per strategy
        self._check_max_positions_per_strategy(signal, state, blocking)

        # 14. Max positions per underlying
        self._check_max_positions_per_underlying(signal, state, blocking)

        # 15. Max open orders
        self._check_max_open_orders(state, blocking)

        # 16. Quote freshness
        self._check_quote_freshness(state, now, blocking)

        # 17. Bid/ask validity and spread
        self._check_quote_validity(state, blocking)

        # 18. Cooldown
        self._check_cooldown(signal, state, now, blocking)

        # ----- Build decision -----
        if blocking:
            return RiskDecision(
                signal_id=signal.signal_id,
                status=RiskDecisionStatus.REJECTED,
                allowed_quantity=0,
                blocking_reasons=blocking,
                warnings=warnings,
            )

        # Approved — compute allowed quantity
        allowed_qty = self._compute_allowed_quantity(signal, warnings)

        return RiskDecision(
            signal_id=signal.signal_id,
            status=RiskDecisionStatus.APPROVED,
            allowed_quantity=allowed_qty,
            blocking_reasons=[],
            warnings=warnings,
        )

    # ---------------------------------------------------------------
    # Individual checks
    # ---------------------------------------------------------------

    def _check_kill_switch(
        self, state: SystemState, blocking: list[str]
    ) -> None:
        if state.kill_switch_engaged:
            blocking.append("kill_switch_engaged")

    def _check_trading_mode(self, blocking: list[str]) -> None:
        mode = self._risk.global_.trading_mode
        if mode == "disabled":
            blocking.append("trading_mode_disabled")

    def _check_system_locked(self, blocking: list[str]) -> None:
        if self._overrides.system_locked:
            blocking.append("system_locked")

    def _check_strategy_enabled(
        self, signal: StrategySignal, blocking: list[str]
    ) -> None:
        strat_cfg = self._strategies.get(signal.strategy_id)
        if strat_cfg is None:
            blocking.append(f"strategy_unknown:{signal.strategy_id}")
            return
        if not strat_cfg.enabled:
            blocking.append(f"strategy_disabled:{signal.strategy_id}")

    def _check_strategy_paused(
        self, signal: StrategySignal, blocking: list[str]
    ) -> None:
        if signal.strategy_id in self._overrides.paused_strategies:
            blocking.append(f"strategy_paused:{signal.strategy_id}")

    def _check_symbol_disabled(
        self, signal: StrategySignal, blocking: list[str]
    ) -> None:
        underlying = signal.contract.symbol
        if underlying in self._overrides.disabled_symbols:
            blocking.append(f"symbol_disabled:{underlying}")

    def _check_reduce_only(
        self, signal: StrategySignal, blocking: list[str]
    ) -> None:
        """Reduce-only rejects new entries. Exits are not signals so they
        don't flow through RiskEngine in the normal path."""
        is_reduce_only = (
            self._overrides.reduce_only
            or signal.strategy_id in self._overrides.reduce_only_strategies
        )
        if is_reduce_only:
            blocking.append("reduce_only_mode")

    def _check_cutoff_time(
        self, now: datetime, blocking: list[str]
    ) -> None:
        cutoff = self._risk.global_.cutoff_time
        current_time_of_day = now.time()
        if current_time_of_day >= cutoff:
            blocking.append(
                f"no_new_trades_cutoff:{self._risk.global_.no_new_trades_cutoff_utc}"
            )

    def _check_daily_loss_limit(
        self, state: SystemState, blocking: list[str]
    ) -> None:
        # daily_pnl is negative when losing money
        if state.daily_pnl <= -self._risk.global_.daily_loss_limit:
            blocking.append(
                f"daily_loss_limit_exceeded:"
                f"pnl={state.daily_pnl},"
                f"limit={self._risk.global_.daily_loss_limit}"
            )

    def _check_strategy_daily_loss_limit(
        self,
        signal: StrategySignal,
        state: SystemState,
        blocking: list[str],
    ) -> None:
        strat_pnl = state.strategy_daily_pnl.get(signal.strategy_id, 0.0)
        limit = self._risk.per_strategy.max_daily_loss
        if strat_pnl <= -limit:
            blocking.append(
                f"strategy_daily_loss_limit_exceeded:"
                f"strategy={signal.strategy_id},"
                f"pnl={strat_pnl},"
                f"limit={limit}"
            )

    def _check_max_contracts(
        self,
        signal: StrategySignal,
        blocking: list[str],
        warnings: list[str],
    ) -> None:
        max_contracts = self._risk.global_.max_contracts_per_trade
        if signal.requested_quantity > max_contracts:
            # This is a warning if we can reduce, but a hard block
            # because the signal itself is oversized. We'll clamp
            # in _compute_allowed_quantity if approved.
            warnings.append(
                f"requested_quantity_exceeds_max_contracts:"
                f"requested={signal.requested_quantity},"
                f"max={max_contracts}"
            )

    def _check_max_open_positions(
        self, state: SystemState, blocking: list[str]
    ) -> None:
        active = self._count_active_positions(state)
        limit = self._risk.global_.max_open_positions
        if active >= limit:
            blocking.append(
                f"max_open_positions_exceeded:count={active},limit={limit}"
            )

    def _check_max_positions_per_strategy(
        self,
        signal: StrategySignal,
        state: SystemState,
        blocking: list[str],
    ) -> None:
        count = sum(
            1
            for p in state.open_positions
            if p.strategy_id == signal.strategy_id
            and p.status in _ACTIVE_POSITION_STATUSES
        )
        limit = self._risk.per_strategy.max_positions
        if count >= limit:
            blocking.append(
                f"max_positions_per_strategy_exceeded:"
                f"strategy={signal.strategy_id},"
                f"count={count},limit={limit}"
            )

    def _check_max_positions_per_underlying(
        self,
        signal: StrategySignal,
        state: SystemState,
        blocking: list[str],
    ) -> None:
        underlying = signal.contract.symbol
        count = sum(
            1
            for p in state.open_positions
            if p.underlying == underlying
            and p.status in _ACTIVE_POSITION_STATUSES
        )
        limit = self._risk.per_underlying.max_positions
        if count >= limit:
            blocking.append(
                f"max_positions_per_underlying_exceeded:"
                f"underlying={underlying},"
                f"count={count},limit={limit}"
            )

    def _check_max_open_orders(
        self, state: SystemState, blocking: list[str]
    ) -> None:
        limit = self._risk.global_.max_open_orders
        if state.open_order_count >= limit:
            blocking.append(
                f"max_open_orders_exceeded:"
                f"count={state.open_order_count},limit={limit}"
            )

    def _check_quote_freshness(
        self,
        state: SystemState,
        now: datetime,
        blocking: list[str],
    ) -> None:
        valid, reason = validate_quote_freshness(
            state.quote, now, self._risk.quote_freshness.max_age_seconds
        )
        if not valid and reason:
            blocking.append(reason)

    def _check_quote_validity(
        self, state: SystemState, blocking: list[str]
    ) -> None:
        if state.quote is None:
            # Already caught by freshness check
            return
        valid, reason = validate_quote_prices(
            state.quote, self._risk.spread_limits.max_spread_pct
        )
        if not valid and reason:
            blocking.append(reason)

    def _check_cooldown(
        self,
        signal: StrategySignal,
        state: SystemState,
        now: datetime,
        blocking: list[str],
    ) -> None:
        strat_cfg = self._strategies.get(signal.strategy_id)
        if strat_cfg is None:
            return  # Already caught by strategy_enabled check

        cooldown_secs = strat_cfg.cooldown_seconds
        if cooldown_secs <= 0:
            return

        last_trade_time = state.last_trade_times.get(signal.strategy_id)
        if last_trade_time is None:
            return

        # Handle tz-aware vs naive
        if last_trade_time.tzinfo is None:
            now_cmp = now.replace(tzinfo=None)
        else:
            now_cmp = now

        elapsed = (now_cmp - last_trade_time).total_seconds()
        if elapsed < cooldown_secs:
            blocking.append(
                f"cooldown_active:strategy={signal.strategy_id},"
                f"elapsed={elapsed:.0f}s,required={cooldown_secs}s"
            )

    # ---------------------------------------------------------------
    # Quantity computation
    # ---------------------------------------------------------------

    def _compute_allowed_quantity(
        self,
        signal: StrategySignal,
        warnings: list[str],
    ) -> int:
        """Compute allowed quantity: may reduce, never increase.

        Per AGENTS.md: RiskEngine may reduce quantity but never increase it.
        """
        qty = signal.requested_quantity
        max_contracts = self._risk.global_.max_contracts_per_trade

        if qty > max_contracts:
            qty = max_contracts

        # max_premium_per_trade: clamp so |limit_price| * multiplier * qty
        # stays within budget. Applies to credit legs too — premium received
        # is a proxy for the notional at risk on a short option.
        max_premium = getattr(self._risk.global_, "max_premium_per_trade", None)
        if max_premium and signal.limit_price:
            per_contract = abs(signal.limit_price) * 100.0
            if per_contract > 0:
                premium_qty = int(max_premium / per_contract)
                if premium_qty < qty:
                    warnings.append(
                        f"quantity_clamped_by_max_premium:"
                        f"requested={qty},allowed={premium_qty},"
                        f"per_contract={per_contract:.2f},limit={max_premium}"
                    )
                    qty = premium_qty

        # Ensure at least 1 if we haven't blocked
        return max(qty, 1)

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _count_active_positions(state: SystemState) -> int:
        """Count positions in active states."""
        return sum(
            1
            for p in state.open_positions
            if p.status in _ACTIVE_POSITION_STATUSES
        )


# Position statuses that count as "active" for limit checks
_ACTIVE_POSITION_STATUSES = frozenset({
    PositionStatus.OPENING,
    PositionStatus.OPEN,
    PositionStatus.PARTIALLY_CLOSED,
})
