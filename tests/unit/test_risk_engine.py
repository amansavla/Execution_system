"""Tests for src.risk.risk_engine — deterministic RiskEngine.

Coverage requirements per AGENTS.md:
- At least one happy path per check
- At least one rejection/failure path per check
- At least one edge case per check

Acceptance criteria tested:
- RiskEngine takes StrategySignal + SystemState → RiskDecision
- Stale quote is rejected with blocking reason
- Wide spread is rejected with blocking reason
- Daily loss limit breach is rejected
- Per-strategy daily loss limit breach is rejected
- Strategy disabled is rejected
- Max open positions exceeded is rejected
- Max positions per strategy exceeded is rejected
- No-new-trades cutoff time is rejected
- Reduce-only mode rejects new entries
- Kill-switch state rejects all new orders
- Approved signal returns allowed_quantity <= requested_quantity
- RiskEngine never calls BrokerClient or any broker module
- RiskEngine never calls OrderManager or PositionManager directly
- Every rejection has a non-empty blocking_reasons list
- All inputs use models/enums from Phase 1 and configs from Phase 2
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.core.config import (
    FullRiskConfig,
    GlobalRiskConfig,
    KillSwitchConfig,
    OverridesConfig,
    PerStrategyRiskConfig,
    PerUnderlyingRiskConfig,
    QuoteFreshnessConfig,
    SpreadLimitsConfig,
    StrategyConfig,
    StrategyEntryConfig,
)
from src.core.enums import (
    OptionRight,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
)
from src.core.models import (
    OptionContract,
    QuoteSnapshot,
    RiskDecision,
    StrategySignal,
)
from src.risk.risk_engine import (
    PositionInfo,
    RiskEngine,
    SystemState,
)


# ===================================================================
# Fixtures — reusable builders
# ===================================================================


def _make_risk_config(**global_overrides) -> FullRiskConfig:
    """Build a valid FullRiskConfig with sensible defaults."""
    global_defaults = {
        "trading_mode": "paper",
        "daily_loss_limit": 5000.0,
        "max_open_positions": 10,
        "max_open_orders": 20,
        "max_contracts_per_trade": 10,
        "max_premium_per_trade": 2000.0,
        "buying_power_reserve_pct": 20.0,
        "no_new_trades_cutoff_utc": "19:30",
        "cooldown_after_loss_seconds": 120,
    }
    global_defaults.update(global_overrides)
    return FullRiskConfig.model_validate({
        "global": global_defaults,
        "per_strategy": {"max_daily_loss": 2000.0, "max_positions": 3},
        "per_underlying": {"max_positions": 5},
        "spread_limits": {"max_spread_pct": 15.0},
        "quote_freshness": {"max_age_seconds": 5.0},
        "kill_switch": {
            "enabled": True,
            "trigger_on_daily_loss": True,
            "trigger_on_disconnect_seconds": 30,
        },
    })


def _make_strategy_config(
    strategy_id: str = "test_strat",
    enabled: bool = True,
    cooldown_seconds: int = 60,
    **kwargs,
) -> StrategyConfig:
    """Build a valid StrategyConfig."""
    return StrategyConfig(
        strategy_id=strategy_id,
        enabled=enabled,
        underlying=kwargs.get("underlying", "SPX"),
        entry=StrategyEntryConfig(
            signal_source="test_scanner",
            max_contracts=kwargs.get("max_contracts", 5),
        ),
        cooldown_seconds=cooldown_seconds,
    )


def _make_contract(**overrides) -> OptionContract:
    defaults = {
        "symbol": "SPX",
        "expiry": "20260520",
        "strike": 5200.0,
        "right": OptionRight.CALL,
    }
    defaults.update(overrides)
    return OptionContract(**defaults)


def _make_signal(**overrides) -> StrategySignal:
    defaults = {
        "strategy_id": "test_strat",
        "direction": SignalDirection.LONG,
        "contract": _make_contract(),
        "requested_quantity": 2,
    }
    defaults.update(overrides)
    return StrategySignal(**defaults)


def _make_quote(**overrides) -> QuoteSnapshot:
    defaults = {
        "symbol": "SPX",
        "bid": 3.50,
        "ask": 3.80,
        "timestamp": datetime.now(UTC),
    }
    defaults.update(overrides)
    return QuoteSnapshot(**defaults)


def _make_engine(
    risk_config: FullRiskConfig | None = None,
    strategy_configs: dict[str, StrategyConfig] | None = None,
    overrides: OverridesConfig | None = None,
) -> RiskEngine:
    """Build a RiskEngine with sensible defaults."""
    if risk_config is None:
        risk_config = _make_risk_config()
    if strategy_configs is None:
        strategy_configs = {"test_strat": _make_strategy_config()}
    if overrides is None:
        overrides = OverridesConfig()
    return RiskEngine(risk_config, strategy_configs, overrides)


def _make_state(**overrides) -> SystemState:
    """Build a SystemState with sensible defaults (clean state)."""
    defaults = {
        "quote": _make_quote(),
        "current_time": datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return SystemState(**defaults)


# ===================================================================
# Happy path tests
# ===================================================================


class TestHappyPath:
    def test_clean_signal_approved(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED
        assert decision.allowed_quantity > 0
        assert decision.blocking_reasons == []

    def test_approved_quantity_lte_requested(self):
        engine = _make_engine()
        signal = _make_signal(requested_quantity=3)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.allowed_quantity <= signal.requested_quantity

    def test_returns_risk_decision_type(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert isinstance(decision, RiskDecision)
        assert decision.signal_id == signal.signal_id

    def test_approved_with_warnings(self):
        """Quantity exceeding max_contracts gets a warning but still approved."""
        engine = _make_engine(risk_config=_make_risk_config(max_contracts_per_trade=2))
        signal = _make_signal(requested_quantity=5)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED
        assert decision.allowed_quantity == 2  # clamped
        assert len(decision.warnings) > 0
        assert any("max_contracts" in w for w in decision.warnings)


# ===================================================================
# Kill switch tests
# ===================================================================


class TestKillSwitch:
    def test_kill_switch_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(kill_switch_engaged=True)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "kill_switch_engaged" in decision.blocking_reasons

    def test_kill_switch_not_engaged_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(kill_switch_engaged=False)
        decision = engine.evaluate(signal, state)
        assert "kill_switch_engaged" not in decision.blocking_reasons


# ===================================================================
# Trading mode tests
# ===================================================================


class TestTradingMode:
    def test_disabled_mode_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(trading_mode="disabled"))
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "trading_mode_disabled" in decision.blocking_reasons

    def test_paper_mode_approves(self):
        engine = _make_engine(risk_config=_make_risk_config(trading_mode="paper"))
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert "trading_mode_disabled" not in decision.blocking_reasons

    def test_live_mode_approves(self):
        engine = _make_engine(risk_config=_make_risk_config(trading_mode="live"))
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert "trading_mode_disabled" not in decision.blocking_reasons


# ===================================================================
# Strategy enabled/paused tests
# ===================================================================


class TestStrategyEnabled:
    def test_disabled_strategy_rejects(self):
        strat = _make_strategy_config(enabled=False)
        engine = _make_engine(strategy_configs={"test_strat": strat})
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("strategy_disabled" in r for r in decision.blocking_reasons)

    def test_unknown_strategy_rejects(self):
        engine = _make_engine(strategy_configs={})
        signal = _make_signal(strategy_id="unknown")
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("strategy_unknown" in r for r in decision.blocking_reasons)

    def test_paused_strategy_rejects(self):
        overrides = OverridesConfig(paused_strategies=["test_strat"])
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("strategy_paused" in r for r in decision.blocking_reasons)

    def test_enabled_strategy_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert not any("strategy_disabled" in r for r in decision.blocking_reasons)


# ===================================================================
# Symbol disabled tests
# ===================================================================


class TestSymbolDisabled:
    def test_disabled_symbol_rejects(self):
        overrides = OverridesConfig(disabled_symbols=["SPX"])
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("symbol_disabled" in r for r in decision.blocking_reasons)

    def test_enabled_symbol_passes(self):
        overrides = OverridesConfig(disabled_symbols=["QQQ"])
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()  # SPX, not QQQ
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert not any("symbol_disabled" in r for r in decision.blocking_reasons)


# ===================================================================
# Reduce-only tests
# ===================================================================


class TestReduceOnly:
    def test_global_reduce_only_rejects_new_entry(self):
        overrides = OverridesConfig(reduce_only=True)
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "reduce_only_mode" in decision.blocking_reasons

    def test_per_strategy_reduce_only_rejects(self):
        overrides = OverridesConfig(reduce_only_strategies=["test_strat"])
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "reduce_only_mode" in decision.blocking_reasons

    def test_reduce_only_other_strategy_passes(self):
        overrides = OverridesConfig(reduce_only_strategies=["other_strat"])
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert "reduce_only_mode" not in decision.blocking_reasons


# ===================================================================
# System locked tests
# ===================================================================


class TestSystemLocked:
    def test_system_locked_rejects(self):
        overrides = OverridesConfig(system_locked=True)
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "system_locked" in decision.blocking_reasons


# ===================================================================
# Cutoff time tests
# ===================================================================


class TestCutoffTime:
    def test_after_cutoff_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(
            no_new_trades_cutoff_utc="15:00"
        ))
        signal = _make_signal()
        # 15:30 is after 15:00 cutoff
        state = _make_state(
            current_time=datetime(2026, 5, 20, 15, 30, 0, tzinfo=UTC)
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("no_new_trades_cutoff" in r for r in decision.blocking_reasons)

    def test_at_cutoff_rejects(self):
        """Exactly at cutoff should reject (>= check)."""
        engine = _make_engine(risk_config=_make_risk_config(
            no_new_trades_cutoff_utc="15:00"
        ))
        signal = _make_signal()
        state = _make_state(
            current_time=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        )
        decision = engine.evaluate(signal, state)
        assert any("no_new_trades_cutoff" in r for r in decision.blocking_reasons)

    def test_before_cutoff_passes(self):
        engine = _make_engine(risk_config=_make_risk_config(
            no_new_trades_cutoff_utc="19:30"
        ))
        signal = _make_signal()
        state = _make_state(
            current_time=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        )
        decision = engine.evaluate(signal, state)
        assert not any("no_new_trades_cutoff" in r for r in decision.blocking_reasons)


# ===================================================================
# Daily loss limit tests
# ===================================================================


class TestDailyLossLimit:
    def test_global_loss_limit_exceeded_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(daily_loss_limit=5000))
        signal = _make_signal()
        state = _make_state(daily_pnl=-5000.0)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("daily_loss_limit_exceeded" in r for r in decision.blocking_reasons)

    def test_global_loss_beyond_limit_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(daily_loss_limit=5000))
        signal = _make_signal()
        state = _make_state(daily_pnl=-6000.0)
        decision = engine.evaluate(signal, state)
        assert any("daily_loss_limit_exceeded" in r for r in decision.blocking_reasons)

    def test_global_loss_within_limit_passes(self):
        engine = _make_engine(risk_config=_make_risk_config(daily_loss_limit=5000))
        signal = _make_signal()
        state = _make_state(daily_pnl=-4999.0)
        decision = engine.evaluate(signal, state)
        assert not any("daily_loss_limit_exceeded" in r for r in decision.blocking_reasons)

    def test_positive_pnl_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(daily_pnl=1000.0)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED


class TestStrategyDailyLossLimit:
    def test_strategy_loss_limit_exceeded_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(
            strategy_daily_pnl={"test_strat": -2000.0}
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any(
            "strategy_daily_loss_limit_exceeded" in r
            for r in decision.blocking_reasons
        )

    def test_strategy_loss_within_limit_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(
            strategy_daily_pnl={"test_strat": -1000.0}
        )
        decision = engine.evaluate(signal, state)
        assert not any(
            "strategy_daily_loss_limit_exceeded" in r
            for r in decision.blocking_reasons
        )

    def test_other_strategy_loss_doesnt_affect(self):
        """Strategy B's loss doesn't block strategy A."""
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(
            strategy_daily_pnl={"other_strat": -5000.0}
        )
        decision = engine.evaluate(signal, state)
        assert not any(
            "strategy_daily_loss_limit_exceeded" in r
            for r in decision.blocking_reasons
        )


# ===================================================================
# Max positions tests
# ===================================================================


class TestMaxPositions:
    def test_max_open_positions_exceeded_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(max_open_positions=2))
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo("s1", "SPX", PositionStatus.OPEN, 1),
            PositionInfo("s2", "QQQ", PositionStatus.OPEN, 1),
        ])
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("max_open_positions_exceeded" in r for r in decision.blocking_reasons)

    def test_closed_positions_dont_count(self):
        engine = _make_engine(risk_config=_make_risk_config(max_open_positions=2))
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo("s1", "SPX", PositionStatus.CLOSED, 1),
            PositionInfo("s2", "QQQ", PositionStatus.CLOSED, 1),
        ])
        decision = engine.evaluate(signal, state)
        assert not any("max_open_positions_exceeded" in r for r in decision.blocking_reasons)

    def test_max_positions_per_strategy_exceeded_rejects(self):
        risk_cfg = _make_risk_config()
        # per_strategy.max_positions is 3
        engine = _make_engine(risk_config=risk_cfg)
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
        ])
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any(
            "max_positions_per_strategy_exceeded" in r
            for r in decision.blocking_reasons
        )

    def test_max_positions_per_underlying_exceeded_rejects(self):
        risk_cfg = _make_risk_config()
        # per_underlying.max_positions is 5
        engine = _make_engine(risk_config=risk_cfg)
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo(f"s{i}", "SPX", PositionStatus.OPEN, 1)
            for i in range(5)
        ])
        decision = engine.evaluate(signal, state)
        assert any(
            "max_positions_per_underlying_exceeded" in r
            for r in decision.blocking_reasons
        )

    def test_different_underlying_doesnt_count(self):
        risk_cfg = _make_risk_config()
        engine = _make_engine(risk_config=risk_cfg)
        signal = _make_signal()  # SPX
        state = _make_state(open_positions=[
            PositionInfo(f"s{i}", "QQQ", PositionStatus.OPEN, 1)
            for i in range(5)
        ])
        decision = engine.evaluate(signal, state)
        assert not any(
            "max_positions_per_underlying_exceeded" in r
            for r in decision.blocking_reasons
        )


# ===================================================================
# Max open orders tests
# ===================================================================


class TestMaxOpenOrders:
    def test_max_open_orders_exceeded_rejects(self):
        engine = _make_engine(risk_config=_make_risk_config(max_open_orders=5))
        signal = _make_signal()
        state = _make_state(open_order_count=5)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("max_open_orders_exceeded" in r for r in decision.blocking_reasons)

    def test_orders_within_limit_passes(self):
        engine = _make_engine(risk_config=_make_risk_config(max_open_orders=20))
        signal = _make_signal()
        state = _make_state(open_order_count=5)
        decision = engine.evaluate(signal, state)
        assert not any("max_open_orders_exceeded" in r for r in decision.blocking_reasons)


# ===================================================================
# Quote freshness tests
# ===================================================================


class TestQuoteFreshness:
    def test_stale_quote_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        stale_quote = _make_quote(
            timestamp=datetime(2026, 5, 20, 14, 59, 0, tzinfo=UTC)
        )
        state = _make_state(
            quote=stale_quote,
            current_time=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("quote_stale" in r for r in decision.blocking_reasons)

    def test_fresh_quote_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        fresh_quote = _make_quote(timestamp=now - timedelta(seconds=2))
        state = _make_state(quote=fresh_quote, current_time=now)
        decision = engine.evaluate(signal, state)
        assert not any("quote_stale" in r for r in decision.blocking_reasons)

    def test_missing_quote_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(quote=None)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "quote_missing" in decision.blocking_reasons

    def test_quote_at_exact_max_age_passes(self):
        """Quote at exactly max_age (5s) should pass (> not >=)."""
        engine = _make_engine()
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        quote = _make_quote(timestamp=now - timedelta(seconds=5))
        state = _make_state(quote=quote, current_time=now)
        decision = engine.evaluate(signal, state)
        assert not any("quote_stale" in r for r in decision.blocking_reasons)

    def test_quote_just_over_max_age_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        quote = _make_quote(timestamp=now - timedelta(seconds=5, milliseconds=1))
        state = _make_state(quote=quote, current_time=now)
        decision = engine.evaluate(signal, state)
        assert any("quote_stale" in r for r in decision.blocking_reasons)


# ===================================================================
# Quote validity and spread tests
# ===================================================================


class TestQuoteValidity:
    def test_missing_bid_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        quote = _make_quote(bid=None)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("quote_incomplete" in r for r in decision.blocking_reasons)

    def test_missing_ask_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        quote = _make_quote(ask=None)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("quote_incomplete" in r for r in decision.blocking_reasons)

    def test_wide_spread_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        # bid=1.0, ask=2.0 → spread=100%, mid=1.5, spread_pct=66.7%
        quote = _make_quote(bid=1.0, ask=2.0)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("spread_too_wide" in r for r in decision.blocking_reasons)

    def test_narrow_spread_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        # bid=3.50, ask=3.80 → spread≈8.2%, under 15%
        quote = _make_quote(bid=3.50, ask=3.80)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert not any("spread_too_wide" in r for r in decision.blocking_reasons)

    def test_spread_at_limit_passes(self):
        """Spread exactly at limit should pass (> not >=)."""
        # For 15% spread: need ask-bid = 15% of mid
        # If mid = 10, spread = 1.5, bid = 9.25, ask = 10.75
        # spread_pct = 1.5/10 * 100 = 15.0%
        quote = _make_quote(bid=9.25, ask=10.75)
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert not any("spread_too_wide" in r for r in decision.blocking_reasons)

    def test_zero_ask_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        quote = _make_quote(bid=0.0, ask=0.0)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED


# ===================================================================
# Cooldown tests
# ===================================================================


class TestCooldown:
    def test_in_cooldown_rejects(self):
        engine = _make_engine()
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        state = _make_state(
            current_time=now,
            last_trade_times={"test_strat": now - timedelta(seconds=30)},
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("cooldown_active" in r for r in decision.blocking_reasons)

    def test_cooldown_expired_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        state = _make_state(
            current_time=now,
            last_trade_times={"test_strat": now - timedelta(seconds=120)},
        )
        decision = engine.evaluate(signal, state)
        assert not any("cooldown_active" in r for r in decision.blocking_reasons)

    def test_no_previous_trade_passes(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(last_trade_times={})
        decision = engine.evaluate(signal, state)
        assert not any("cooldown_active" in r for r in decision.blocking_reasons)

    def test_zero_cooldown_passes(self):
        strat = _make_strategy_config(cooldown_seconds=0)
        engine = _make_engine(strategy_configs={"test_strat": strat})
        signal = _make_signal()
        now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)
        state = _make_state(
            current_time=now,
            last_trade_times={"test_strat": now - timedelta(seconds=1)},
        )
        decision = engine.evaluate(signal, state)
        assert not any("cooldown_active" in r for r in decision.blocking_reasons)


# ===================================================================
# Quantity clamping tests
# ===================================================================


class TestQuantityClamping:
    def test_quantity_clamped_to_max_contracts(self):
        engine = _make_engine(risk_config=_make_risk_config(max_contracts_per_trade=3))
        signal = _make_signal(requested_quantity=10)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED
        assert decision.allowed_quantity == 3

    def test_quantity_within_limit_unchanged(self):
        engine = _make_engine(risk_config=_make_risk_config(max_contracts_per_trade=10))
        signal = _make_signal(requested_quantity=5)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.allowed_quantity == 5

    def test_allowed_quantity_never_exceeds_requested(self):
        engine = _make_engine()
        signal = _make_signal(requested_quantity=1)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.allowed_quantity <= signal.requested_quantity

    def test_quantity_clamped_by_max_premium(self):
        # 10 lots at $2.91 = $2910 premium > $2000 budget -> clamp to 6
        engine = _make_engine(risk_config=_make_risk_config(max_premium_per_trade=2000.0))
        signal = _make_signal(requested_quantity=10, limit_price=2.91)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED
        assert decision.allowed_quantity == 6
        assert any("quantity_clamped_by_max_premium" in w for w in decision.warnings)

    def test_premium_within_budget_not_clamped(self):
        engine = _make_engine(risk_config=_make_risk_config(max_premium_per_trade=2000.0))
        signal = _make_signal(requested_quantity=5, limit_price=2.91)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.allowed_quantity == 5

    def test_no_limit_price_skips_premium_clamp(self):
        engine = _make_engine(risk_config=_make_risk_config(max_premium_per_trade=2000.0))
        signal = _make_signal(requested_quantity=10, limit_price=None)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.allowed_quantity == 10


# ===================================================================
# Multiple blocking reasons tests
# ===================================================================


class TestMultipleBlockingReasons:
    def test_collects_all_blocking_reasons(self):
        """Multiple violations should all appear in blocking_reasons."""
        overrides = OverridesConfig(
            system_locked=True,
            disabled_symbols=["SPX"],
            reduce_only=True,
        )
        engine = _make_engine(
            risk_config=_make_risk_config(trading_mode="disabled"),
            overrides=overrides,
        )
        signal = _make_signal()
        state = _make_state(
            kill_switch_engaged=True,
            daily_pnl=-10000.0,
            quote=None,
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert len(decision.blocking_reasons) >= 5
        assert "kill_switch_engaged" in decision.blocking_reasons
        assert "trading_mode_disabled" in decision.blocking_reasons
        assert "system_locked" in decision.blocking_reasons
        assert any("symbol_disabled" in r for r in decision.blocking_reasons)
        assert "reduce_only_mode" in decision.blocking_reasons

    def test_every_rejection_has_nonempty_blocking_reasons(self):
        """A rejected decision must always have at least one blocking reason."""
        # Test with each individual check
        test_cases = [
            ("kill_switch", _make_state(kill_switch_engaged=True)),
            ("quote_missing", _make_state(quote=None)),
            ("stale_quote", _make_state(
                quote=_make_quote(timestamp=datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)),
                current_time=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
            )),
        ]
        engine = _make_engine()
        signal = _make_signal()
        for name, state in test_cases:
            decision = engine.evaluate(signal, state)
            if decision.status == RiskDecisionStatus.REJECTED:
                assert len(decision.blocking_reasons) > 0, (
                    f"Rejection for {name} has empty blocking_reasons"
                )


# ===================================================================
# No forbidden imports tests
# ===================================================================


class TestNoForbiddenImports:
    def test_no_broker_imports(self):
        """RiskEngine must not import any broker modules."""
        import inspect
        import src.risk.risk_engine as re_module
        source = inspect.getsource(re_module)

        # Check IBKR-specific packages never imported
        assert "ib_async" not in source
        assert "ib_insync" not in source
        assert "ibapi" not in source

        # Check actual import lines for forbidden modules
        for line in source.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                assert "broker" not in stripped.lower(), (
                    f"Forbidden broker import found: {stripped}"
                )
                assert "order_manager" not in stripped.lower(), (
                    f"Forbidden OrderManager import found: {stripped}"
                )
                assert "position_manager" not in stripped.lower(), (
                    f"Forbidden PositionManager import found: {stripped}"
                )


# ===================================================================
# Acceptance criteria — explicit
# ===================================================================


class TestAcceptanceCriteria:
    """Explicit tests mapping to each acceptance criterion."""

    def test_takes_signal_and_state_returns_decision(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert isinstance(decision, RiskDecision)
        assert decision.signal_id == signal.signal_id

    def test_stale_quote_rejected_with_blocking_reason(self):
        engine = _make_engine()
        signal = _make_signal()
        stale_quote = _make_quote(
            timestamp=datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)
        )
        state = _make_state(
            quote=stale_quote,
            current_time=datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC),
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("quote_stale" in r for r in decision.blocking_reasons)

    def test_wide_spread_rejected_with_blocking_reason(self):
        engine = _make_engine()
        signal = _make_signal()
        quote = _make_quote(bid=1.0, ask=3.0)
        state = _make_state(quote=quote)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("spread_too_wide" in r for r in decision.blocking_reasons)

    def test_daily_loss_limit_breach_rejected(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(daily_pnl=-5000.0)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("daily_loss_limit" in r for r in decision.blocking_reasons)

    def test_per_strategy_daily_loss_breach_rejected(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(strategy_daily_pnl={"test_strat": -2000.0})
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any(
            "strategy_daily_loss_limit" in r for r in decision.blocking_reasons
        )

    def test_strategy_disabled_rejected(self):
        strat = _make_strategy_config(enabled=False)
        engine = _make_engine(strategy_configs={"test_strat": strat})
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("strategy_disabled" in r for r in decision.blocking_reasons)

    def test_max_open_positions_exceeded_rejected(self):
        engine = _make_engine(risk_config=_make_risk_config(max_open_positions=1))
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo("s1", "SPX", PositionStatus.OPEN, 1),
        ])
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("max_open_positions" in r for r in decision.blocking_reasons)

    def test_max_positions_per_strategy_exceeded_rejected(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(open_positions=[
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
            PositionInfo("test_strat", "SPX", PositionStatus.OPEN, 1),
        ])
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any(
            "max_positions_per_strategy" in r for r in decision.blocking_reasons
        )

    def test_cutoff_time_rejected(self):
        engine = _make_engine(risk_config=_make_risk_config(
            no_new_trades_cutoff_utc="15:00"
        ))
        signal = _make_signal()
        state = _make_state(
            current_time=datetime(2026, 5, 20, 16, 0, 0, tzinfo=UTC)
        )
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert any("no_new_trades_cutoff" in r for r in decision.blocking_reasons)

    def test_reduce_only_rejects_new_entries(self):
        overrides = OverridesConfig(reduce_only=True)
        engine = _make_engine(overrides=overrides)
        signal = _make_signal()
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "reduce_only_mode" in decision.blocking_reasons

    def test_kill_switch_rejects_all_new_orders(self):
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state(kill_switch_engaged=True)
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert "kill_switch_engaged" in decision.blocking_reasons

    def test_approved_quantity_lte_requested(self):
        engine = _make_engine()
        signal = _make_signal(requested_quantity=5)
        state = _make_state()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.APPROVED
        assert decision.allowed_quantity <= signal.requested_quantity

    def test_every_rejection_has_nonempty_blocking_reasons(self):
        """No rejection should ever have an empty blocking_reasons list."""
        engine = _make_engine()
        # Create a state that will be rejected
        state = _make_state(kill_switch_engaged=True)
        signal = _make_signal()
        decision = engine.evaluate(signal, state)
        assert decision.status == RiskDecisionStatus.REJECTED
        assert len(decision.blocking_reasons) > 0

    def test_all_inputs_use_phase1_models(self):
        """Verify the evaluate method uses models from Phase 1."""
        engine = _make_engine()
        signal = _make_signal()
        state = _make_state()
        # These are Phase 1 types
        assert isinstance(signal, StrategySignal)
        decision = engine.evaluate(signal, state)
        assert isinstance(decision, RiskDecision)
