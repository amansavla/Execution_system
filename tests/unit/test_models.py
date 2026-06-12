"""Tests for src.core.models and src.core.enums.

Coverage requirements per AGENTS.md:
- At least one happy path per model
- At least one rejection/failure path per model
- At least one edge case per model
- Tests must not only test construction

Acceptance criteria tested:
- Zero or negative quantity rejected at construction
- Invalid OptionRight rejected
- Bid greater than ask rejected
- Missing bid or ask represented and detectable on QuoteSnapshot
- Stale quote detectable via timestamp on QuoteSnapshot
- All order lifecycle states present in OrderStatus
- All position lifecycle states present in PositionStatus
- No IBKR imports anywhere
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.enums import (
    OptionRight,
    OrderSide,
    OrderStatus,
    PositionStatus,
    RiskDecisionStatus,
    SignalDirection,
    TradingMode,
)
from src.core.models import (
    AccountState,
    ExecutionReport,
    ExitRule,
    FillEvent,
    ManualOverride,
    OptionContract,
    OrderEvent,
    OrderIntent,
    OrderPlan,
    OrderState,
    Position,
    QuoteSnapshot,
    ReconciliationReport,
    RiskConfig,
    RiskDecision,
    StrategySignal,
)


# ===================================================================
# Helpers
# ===================================================================

def _make_contract(**overrides) -> OptionContract:
    """Build a valid OptionContract with sensible defaults."""
    defaults = {
        "symbol": "SPX",
        "expiry": "20260520",
        "strike": 5200.0,
        "right": OptionRight.CALL,
    }
    defaults.update(overrides)
    return OptionContract(**defaults)


def _make_quote(**overrides) -> QuoteSnapshot:
    """Build a valid QuoteSnapshot with sensible defaults."""
    defaults = {
        "symbol": "SPX",
        "bid": 3.50,
        "ask": 3.80,
        "timestamp": datetime.now(UTC),
    }
    defaults.update(overrides)
    return QuoteSnapshot(**defaults)


def _make_signal(**overrides) -> StrategySignal:
    """Build a valid StrategySignal with sensible defaults."""
    defaults = {
        "strategy_id": "test_strat",
        "direction": SignalDirection.LONG,
        "contract": _make_contract(),
        "requested_quantity": 1,
    }
    defaults.update(overrides)
    return StrategySignal(**defaults)


# ===================================================================
# Enum tests
# ===================================================================


class TestOrderStatusEnum:
    """All order lifecycle states from AGENTS.md must be present."""

    def test_all_states_present(self):
        expected = {
            "NEW", "RISK_CHECKED", "SUBMITTED", "PARTIALLY_FILLED",
            "FILLED", "CANCEL_PENDING", "CANCELLED", "REJECTED", "ERROR",
        }
        actual = {s.value for s in OrderStatus}
        assert actual == expected

    def test_string_comparison(self):
        assert OrderStatus.NEW == "NEW"
        assert OrderStatus.FILLED != "NEW"

    def test_invalid_state_raises(self):
        with pytest.raises(ValueError):
            OrderStatus("UNKNOWN_STATE")


class TestPositionStatusEnum:
    """All position lifecycle states from AGENTS.md must be present."""

    def test_all_states_present(self):
        expected = {
            "OPENING", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "FORCE_CLOSED",
        }
        actual = {s.value for s in PositionStatus}
        assert actual == expected

    def test_string_comparison(self):
        assert PositionStatus.OPEN == "OPEN"

    def test_lifecycle_order_is_representable(self):
        """Verify the lifecycle sequence can be expressed."""
        lifecycle = [
            PositionStatus.OPENING,
            PositionStatus.OPEN,
            PositionStatus.PARTIALLY_CLOSED,
            PositionStatus.CLOSED,
        ]
        assert len(lifecycle) == 4
        assert lifecycle[0] == PositionStatus.OPENING
        assert lifecycle[-1] == PositionStatus.CLOSED


class TestOptionRightEnum:
    def test_valid_values(self):
        assert OptionRight.CALL == "CALL"
        assert OptionRight.PUT == "PUT"

    def test_invalid_right_rejected(self):
        with pytest.raises(ValueError):
            OptionRight("STRADDLE")


class TestTradingModeEnum:
    def test_all_modes(self):
        assert {m.value for m in TradingMode} == {"PAPER", "LIVE", "DISABLED"}


class TestSignalDirectionEnum:
    def test_all_directions(self):
        assert {d.value for d in SignalDirection} == {"LONG", "SHORT"}


class TestRiskDecisionStatusEnum:
    def test_all_statuses(self):
        assert {s.value for s in RiskDecisionStatus} == {"APPROVED", "REJECTED"}


# ===================================================================
# OptionContract tests
# ===================================================================


class TestOptionContract:
    def test_happy_path(self):
        c = _make_contract()
        assert c.symbol == "SPX"
        assert c.strike == 5200.0
        assert c.right == OptionRight.CALL
        assert c.multiplier == 100

    def test_reject_empty_symbol(self):
        with pytest.raises(ValidationError):
            _make_contract(symbol="")

    def test_reject_zero_strike(self):
        with pytest.raises(ValidationError):
            _make_contract(strike=0)

    def test_reject_negative_strike(self):
        with pytest.raises(ValidationError):
            _make_contract(strike=-100)

    def test_reject_invalid_expiry_format(self):
        with pytest.raises(ValidationError):
            _make_contract(expiry="2026-05-20")

    def test_reject_invalid_right(self):
        with pytest.raises(ValidationError):
            _make_contract(right="STRADDLE")

    def test_edge_case_small_strike(self):
        """Sub-dollar strikes should be valid (penny options exist)."""
        c = _make_contract(strike=0.50)
        assert c.strike == 0.50


# ===================================================================
# QuoteSnapshot tests
# ===================================================================


class TestQuoteSnapshot:
    def test_happy_path(self):
        q = _make_quote()
        assert q.bid == 3.50
        assert q.ask == 3.80
        assert q.symbol == "SPX"

    def test_bid_greater_than_ask_rejected(self):
        with pytest.raises(ValidationError, match="bid.*must not be greater than ask"):
            _make_quote(bid=4.00, ask=3.50)

    def test_missing_bid_is_none_and_detectable(self):
        q = _make_quote(bid=None)
        assert q.bid is None
        assert q.ask is not None

    def test_missing_ask_is_none_and_detectable(self):
        q = _make_quote(ask=None)
        assert q.ask is None
        assert q.bid is not None

    def test_both_bid_and_ask_missing(self):
        q = _make_quote(bid=None, ask=None)
        assert q.bid is None
        assert q.ask is None

    def test_stale_quote_detectable_via_timestamp(self):
        """A quote from 10 seconds ago should be detectable as stale."""
        old_time = datetime.now(UTC) - timedelta(seconds=10)
        q = _make_quote(timestamp=old_time)
        age = (datetime.now(UTC) - q.timestamp).total_seconds()
        assert age >= 9  # at least 9 seconds old (allow clock jitter)

    def test_fresh_quote_detectable(self):
        q = _make_quote(timestamp=datetime.now(UTC))
        age = (datetime.now(UTC) - q.timestamp).total_seconds()
        assert age < 2  # definitely fresh

    def test_bid_equals_ask_allowed(self):
        """Locked market (bid == ask) is valid."""
        q = _make_quote(bid=3.50, ask=3.50)
        assert q.bid == q.ask

    def test_zero_bid_with_positive_ask_allowed(self):
        """A zero bid (worthless option bid side) is valid."""
        q = _make_quote(bid=0.0, ask=0.05)
        assert q.bid == 0.0


# ===================================================================
# StrategySignal tests
# ===================================================================


class TestStrategySignal:
    def test_happy_path(self):
        s = _make_signal()
        assert s.strategy_id == "test_strat"
        assert s.requested_quantity == 1
        assert s.direction == SignalDirection.LONG
        assert s.signal_id is not None

    def test_reject_zero_quantity(self):
        with pytest.raises(ValidationError):
            _make_signal(requested_quantity=0)

    def test_reject_negative_quantity(self):
        with pytest.raises(ValidationError):
            _make_signal(requested_quantity=-5)

    def test_optional_exit_fields_default_none(self):
        s = _make_signal()
        assert s.stop_price is None
        assert s.take_profit_price is None
        assert s.time_exit_utc is None

    def test_signal_with_exit_params(self):
        exit_time = datetime.now(UTC) + timedelta(hours=2)
        s = _make_signal(
            stop_price=5100.0,
            take_profit_price=5300.0,
            time_exit_utc=exit_time,
        )
        assert s.stop_price == 5100.0
        assert s.take_profit_price == 5300.0
        assert s.time_exit_utc == exit_time

    def test_metadata_default_empty_dict(self):
        s = _make_signal()
        assert s.metadata == {}

    def test_metadata_accepts_arbitrary_data(self):
        s = _make_signal(metadata={"reason": "momentum", "score": 0.95})
        assert s.metadata["reason"] == "momentum"


# ===================================================================
# AccountState tests
# ===================================================================


class TestAccountState:
    def test_happy_path(self):
        a = AccountState(
            account_id="DU12345",
            net_liquidation=100000.0,
            available_funds=50000.0,
            buying_power=200000.0,
        )
        assert a.account_id == "DU12345"
        assert a.daily_pnl == 0.0

    def test_reject_empty_account_id(self):
        with pytest.raises(ValidationError):
            AccountState(
                account_id="",
                net_liquidation=100000.0,
                available_funds=50000.0,
                buying_power=200000.0,
            )

    def test_negative_available_funds_allowed(self):
        """Account can have negative available funds (margin usage)."""
        a = AccountState(
            account_id="DU12345",
            net_liquidation=100000.0,
            available_funds=-5000.0,
            buying_power=0.0,
        )
        assert a.available_funds == -5000.0


# ===================================================================
# RiskConfig tests
# ===================================================================


class TestRiskConfig:
    def test_happy_path(self):
        rc = RiskConfig(
            daily_loss_limit=5000.0,
            max_open_positions=10,
            max_open_orders=20,
            max_contracts_per_trade=10,
            max_premium_per_trade=2000.0,
            max_spread_pct=15.0,
            quote_max_age_seconds=5.0,
        )
        assert rc.daily_loss_limit == 5000.0

    def test_reject_zero_daily_loss_limit(self):
        with pytest.raises(ValidationError):
            RiskConfig(
                daily_loss_limit=0,
                max_open_positions=10,
                max_open_orders=20,
                max_contracts_per_trade=10,
                max_premium_per_trade=2000.0,
                max_spread_pct=15.0,
                quote_max_age_seconds=5.0,
            )

    def test_reject_negative_max_positions(self):
        with pytest.raises(ValidationError):
            RiskConfig(
                daily_loss_limit=5000.0,
                max_open_positions=-1,
                max_open_orders=20,
                max_contracts_per_trade=10,
                max_premium_per_trade=2000.0,
                max_spread_pct=15.0,
                quote_max_age_seconds=5.0,
            )


# ===================================================================
# RiskDecision tests
# ===================================================================


class TestRiskDecision:
    def test_approved_decision(self):
        rd = RiskDecision(
            signal_id=uuid4(),
            status=RiskDecisionStatus.APPROVED,
            allowed_quantity=5,
        )
        assert rd.status == RiskDecisionStatus.APPROVED
        assert rd.blocking_reasons == []

    def test_rejected_decision_with_reasons(self):
        rd = RiskDecision(
            signal_id=uuid4(),
            status=RiskDecisionStatus.REJECTED,
            allowed_quantity=0,
            blocking_reasons=["daily_loss_limit_exceeded", "symbol_disabled"],
            warnings=["spread_near_limit"],
        )
        assert rd.status == RiskDecisionStatus.REJECTED
        assert len(rd.blocking_reasons) == 2
        assert "daily_loss_limit_exceeded" in rd.blocking_reasons

    def test_zero_allowed_quantity_valid_for_rejection(self):
        rd = RiskDecision(
            signal_id=uuid4(),
            status=RiskDecisionStatus.REJECTED,
            allowed_quantity=0,
        )
        assert rd.allowed_quantity == 0


# ===================================================================
# OrderIntent tests
# ===================================================================


class TestOrderIntent:
    def test_happy_path(self):
        oi = OrderIntent(
            signal_id=uuid4(),
            risk_decision_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=3,
            limit_price=3.60,
        )
        assert oi.quantity == 3
        assert oi.side == OrderSide.BUY

    def test_reject_zero_quantity(self):
        with pytest.raises(ValidationError):
            OrderIntent(
                signal_id=uuid4(),
                risk_decision_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=0,
                limit_price=3.60,
            )

    def test_reject_negative_quantity(self):
        with pytest.raises(ValidationError):
            OrderIntent(
                signal_id=uuid4(),
                risk_decision_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.SELL,
                quantity=-1,
                limit_price=3.60,
            )


# ===================================================================
# OrderPlan tests
# ===================================================================


class TestOrderPlan:
    def test_happy_path(self):
        op = OrderPlan(
            order_intent_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            limit_price=3.60,
        )
        assert op.order_type == "LMT"
        assert op.time_in_force == "DAY"

    def test_reject_zero_quantity(self):
        with pytest.raises(ValidationError):
            OrderPlan(
                order_intent_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=0,
                limit_price=3.60,
            )

    def test_custom_order_type(self):
        """Order type can be overridden (e.g., for emergency MKT orders)."""
        op = OrderPlan(
            order_intent_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.SELL,
            quantity=1,
            limit_price=3.60,
            order_type="MKT",
        )
        assert op.order_type == "MKT"


# ===================================================================
# OrderState tests
# ===================================================================


class TestOrderState:
    def test_happy_path(self):
        os_ = OrderState(
            order_plan_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            limit_price=3.60,
        )
        assert os_.status == OrderStatus.NEW
        assert os_.filled_quantity == 0

    def test_reject_zero_quantity(self):
        with pytest.raises(ValidationError):
            OrderState(
                order_plan_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=0,
                limit_price=3.60,
            )

    def test_partial_fill_state_representable(self):
        """An order can be partially filled with filled_qty < quantity."""
        os_ = OrderState(
            order_plan_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=10,
            filled_quantity=3,
            limit_price=3.60,
            status=OrderStatus.PARTIALLY_FILLED,
        )
        assert os_.filled_quantity < os_.quantity
        assert os_.status == OrderStatus.PARTIALLY_FILLED


# ===================================================================
# OrderEvent tests
# ===================================================================


class TestOrderEvent:
    def test_happy_path(self):
        oe = OrderEvent(
            order_id=uuid4(),
            new_status=OrderStatus.SUBMITTED,
            previous_status=OrderStatus.RISK_CHECKED,
            message="Order submitted to broker",
        )
        assert oe.new_status == OrderStatus.SUBMITTED

    def test_first_event_has_no_previous(self):
        oe = OrderEvent(
            order_id=uuid4(),
            new_status=OrderStatus.NEW,
        )
        assert oe.previous_status is None

    def test_error_event_with_message(self):
        oe = OrderEvent(
            order_id=uuid4(),
            new_status=OrderStatus.ERROR,
            previous_status=OrderStatus.SUBMITTED,
            message="Broker connection lost",
        )
        assert oe.new_status == OrderStatus.ERROR
        assert "connection" in oe.message.lower()


# ===================================================================
# FillEvent tests
# ===================================================================


class TestFillEvent:
    def test_happy_path(self):
        fe = FillEvent(
            order_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            filled_quantity=5,
            fill_price=3.55,
            commission=1.25,
        )
        assert fe.filled_quantity == 5
        assert fe.commission == 1.25

    def test_reject_zero_filled_quantity(self):
        with pytest.raises(ValidationError):
            FillEvent(
                order_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                filled_quantity=0,
                fill_price=3.55,
            )

    def test_commission_none_when_not_reported(self):
        """IBKR may not report commission with the fill (P1-A4)."""
        fe = FillEvent(
            order_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            filled_quantity=1,
            fill_price=3.55,
        )
        assert fe.commission is None


# ===================================================================
# Position tests
# ===================================================================


class TestPosition:
    def test_happy_path(self):
        p = Position(
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            average_entry_price=3.55,
            entry_order_id=uuid4(),
        )
        assert p.status == PositionStatus.OPENING
        assert p.realized_pnl == 0.0

    def test_reject_zero_quantity(self):
        with pytest.raises(ValidationError):
            Position(
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=0,
                average_entry_price=3.55,
                entry_order_id=uuid4(),
            )

    def test_reject_negative_quantity(self):
        with pytest.raises(ValidationError):
            Position(
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                quantity=-1,
                average_entry_price=3.55,
                entry_order_id=uuid4(),
            )

    def test_force_closed_state_representable(self):
        p = Position(
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=5,
            average_entry_price=3.55,
            status=PositionStatus.FORCE_CLOSED,
            entry_order_id=uuid4(),
        )
        assert p.status == PositionStatus.FORCE_CLOSED

    def test_partially_closed_with_exit_orders(self):
        exit_id = uuid4()
        p = Position(
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            quantity=10,
            filled_quantity=7,
            average_entry_price=3.55,
            status=PositionStatus.PARTIALLY_CLOSED,
            entry_order_id=uuid4(),
            exit_order_ids=[exit_id],
        )
        assert p.status == PositionStatus.PARTIALLY_CLOSED
        assert len(p.exit_order_ids) == 1


# ===================================================================
# ExitRule tests
# ===================================================================


class TestExitRule:
    def test_happy_path(self):
        er = ExitRule(
            position_id=uuid4(),
            stop_price=5100.0,
            take_profit_price=5300.0,
        )
        assert er.stop_price == 5100.0

    def test_no_exit_params(self):
        """All exit fields optional — ExitManager uses config defaults."""
        er = ExitRule(position_id=uuid4())
        assert er.stop_price is None
        assert er.take_profit_price is None
        assert er.time_exit_utc is None
        assert er.trailing_stop is False

    def test_time_exit(self):
        exit_time = datetime(2026, 5, 20, 19, 45)
        er = ExitRule(position_id=uuid4(), time_exit_utc=exit_time)
        assert er.time_exit_utc == exit_time


# ===================================================================
# ManualOverride tests
# ===================================================================


class TestManualOverride:
    def test_happy_path(self):
        mo = ManualOverride(
            command="pause-strategy",
            target="test_strat",
        )
        assert mo.command == "pause-strategy"
        assert mo.target == "test_strat"

    def test_reject_empty_command(self):
        with pytest.raises(ValidationError):
            ManualOverride(command="")

    def test_system_wide_command_no_target(self):
        mo = ManualOverride(command="lock-system")
        assert mo.target is None
        assert mo.operator == "system"


# ===================================================================
# ExecutionReport tests
# ===================================================================


class TestExecutionReport:
    def test_happy_path(self):
        er = ExecutionReport(
            order_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            requested_quantity=5,
            filled_quantity=5,
            average_fill_price=3.55,
            total_commission=6.25,
            slippage=0.02,
            fill_time_seconds=1.5,
        )
        assert er.filled_quantity == er.requested_quantity

    def test_partial_fill_report(self):
        er = ExecutionReport(
            order_id=uuid4(),
            strategy_id="test_strat",
            contract=_make_contract(),
            side=OrderSide.BUY,
            requested_quantity=10,
            filled_quantity=3,
        )
        assert er.filled_quantity < er.requested_quantity
        assert er.average_fill_price is None

    def test_reject_zero_requested_quantity(self):
        with pytest.raises(ValidationError):
            ExecutionReport(
                order_id=uuid4(),
                strategy_id="test_strat",
                contract=_make_contract(),
                side=OrderSide.BUY,
                requested_quantity=0,
                filled_quantity=0,
            )


# ===================================================================
# ReconciliationReport tests
# ===================================================================


class TestReconciliationReport:
    def test_clean_reconciliation(self):
        rr = ReconciliationReport(
            matches=5,
            mismatches=0,
            is_clean=True,
        )
        assert rr.is_clean
        assert rr.mismatches == 0

    def test_dirty_reconciliation(self):
        internal_id = uuid4()
        rr = ReconciliationReport(
            matches=4,
            mismatches=1,
            internal_only=[internal_id],
            broker_only=["IBKR-POS-999"],
            is_clean=False,
        )
        assert not rr.is_clean
        assert len(rr.internal_only) == 1
        assert len(rr.broker_only) == 1

    def test_empty_report(self):
        """Edge case: no positions at all is a clean reconciliation."""
        rr = ReconciliationReport()
        assert rr.matches == 0
        assert rr.mismatches == 0
        assert rr.is_clean


# ===================================================================
# Cross-cutting acceptance criteria
# ===================================================================


class TestAcceptanceCriteria:
    """Explicit tests for every acceptance criterion in the task spec."""

    def test_zero_quantity_rejected_at_construction(self):
        with pytest.raises(ValidationError):
            _make_signal(requested_quantity=0)

    def test_negative_quantity_rejected_at_construction(self):
        with pytest.raises(ValidationError):
            _make_signal(requested_quantity=-3)

    def test_invalid_option_right_rejected(self):
        with pytest.raises(ValidationError):
            _make_contract(right="STRADDLE")

    def test_bid_greater_than_ask_rejected(self):
        with pytest.raises(ValidationError):
            _make_quote(bid=10.0, ask=5.0)

    def test_missing_bid_represented_and_detectable(self):
        q = _make_quote(bid=None)
        assert q.bid is None
        has_bid = q.bid is not None
        assert has_bid is False

    def test_missing_ask_represented_and_detectable(self):
        q = _make_quote(ask=None)
        assert q.ask is None
        has_ask = q.ask is not None
        assert has_ask is False

    def test_stale_quote_detectable_via_timestamp(self):
        stale_time = datetime.now(UTC) - timedelta(seconds=60)
        q = _make_quote(timestamp=stale_time)
        max_age_seconds = 5
        age = (datetime.now(UTC) - q.timestamp).total_seconds()
        is_stale = age > max_age_seconds
        assert is_stale

    def test_all_order_lifecycle_states_in_enum(self):
        required = {
            "NEW", "RISK_CHECKED", "SUBMITTED", "PARTIALLY_FILLED",
            "FILLED", "CANCEL_PENDING", "CANCELLED", "REJECTED", "ERROR",
        }
        actual = {s.value for s in OrderStatus}
        assert required.issubset(actual)

    def test_all_position_lifecycle_states_in_enum(self):
        required = {
            "OPENING", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "FORCE_CLOSED",
        }
        actual = {s.value for s in PositionStatus}
        assert required.issubset(actual)

    def test_no_ibkr_imports_in_enums(self):
        """Verify enums module has no IBKR imports."""
        import src.core.enums as enums_module
        import inspect
        source = inspect.getsource(enums_module)
        assert "ib_async" not in source
        assert "ib_insync" not in source
        assert "ibapi" not in source

    def test_no_ibkr_imports_in_models(self):
        """Verify models module has no IBKR imports."""
        import src.core.models as models_module
        import inspect
        source = inspect.getsource(models_module)
        assert "ib_async" not in source
        assert "ib_insync" not in source
        assert "ibapi" not in source
