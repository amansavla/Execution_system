"""Tests for src.core.config — config loading and validation.

Coverage requirements per AGENTS.md:
- At least one happy path per config
- At least one rejection/failure path per config
- At least one edge case per config

Acceptance criteria tested:
- Malformed YAML fails with a clear validation error
- live_trading.enabled defaults to false; true requires explicit set
- Duplicate strategy IDs are rejected
- Risk limits must be positive numbers
- no_new_trades_cutoff and force_flatten_time must be valid times
  and force_flatten must be after no_new_trades_cutoff
- Missing required fields fail loudly, not silently
"""

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from src.core.config import (
    BrokerConfig,
    DashboardConfig,
    FullRiskConfig,
    GlobalRiskConfig,
    OverridesConfig,
    StrategiesConfig,
    StrategyConfig,
    SymbolConfig,
    SymbolsConfig,
    TradingHoursConfig,
    load_broker_config,
    load_dashboard_config,
    load_overrides_config,
    load_risk_config,
    load_strategies_config,
    load_symbols_config,
)


# ===================================================================
# Fixtures — write temp YAML files
# ===================================================================


def _write_yaml(tmp_path: Path, filename: str, content: str) -> Path:
    """Write YAML content to a temp file and return its path."""
    p = tmp_path / filename
    p.write_text(dedent(content))
    return p


# ===================================================================
# Broker config tests
# ===================================================================


class TestBrokerConfig:
    def test_happy_path_loads_defaults(self, tmp_path):
        p = _write_yaml(tmp_path, "broker.yaml", """\
            live_trading:
              enabled: false
        """)
        cfg = load_broker_config(p)
        assert cfg.live_trading.enabled is False
        assert cfg.connection.host == "127.0.0.1"
        assert cfg.connection.port == 7497
        assert cfg.order_defaults.order_type == "LMT"

    def test_live_trading_defaults_to_false(self, tmp_path):
        """An empty broker.yaml should default live_trading.enabled to false."""
        p = _write_yaml(tmp_path, "broker.yaml", "# empty config\n")
        cfg = load_broker_config(p)
        assert cfg.live_trading.enabled is False

    def test_live_trading_explicit_true(self, tmp_path):
        p = _write_yaml(tmp_path, "broker.yaml", """\
            live_trading:
              enabled: true
        """)
        cfg = load_broker_config(p)
        assert cfg.live_trading.enabled is True

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_broker_config(tmp_path / "nonexistent.yaml")

    def test_malformed_yaml_raises(self, tmp_path):
        p = _write_yaml(tmp_path, "broker.yaml", """\
            live_trading:
              enabled: [this is: {broken yaml
        """)
        with pytest.raises(ValueError, match="Malformed YAML"):
            load_broker_config(p)

    def test_invalid_port_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "broker.yaml", """\
            connection:
              port: 99999
        """)
        with pytest.raises(ValidationError):
            load_broker_config(p)

    def test_full_config_with_all_sections(self, tmp_path):
        p = _write_yaml(tmp_path, "broker.yaml", """\
            live_trading:
              enabled: false
            connection:
              host: "127.0.0.1"
              port: 7497
              client_id: 1
              timeout_seconds: 30
              readonly: false
            account:
              account_id: "DU12345"
              allowlist:
                - "DU12345"
            reconnection:
              enabled: true
              max_retries: 5
              retry_delay_seconds: 10
              stale_on_disconnect: true
            order_defaults:
              order_type: "LMT"
              time_in_force: "DAY"
              transmit: true
        """)
        cfg = load_broker_config(p)
        assert cfg.account.account_id == "DU12345"
        assert cfg.reconnection.max_retries == 5


# ===================================================================
# Risk config tests
# ===================================================================


class TestRiskConfig:
    VALID_RISK_YAML = """\
        global:
          trading_mode: "paper"
          daily_loss_limit: 5000.00
          max_open_positions: 10
          max_open_orders: 20
          max_contracts_per_trade: 10
          max_premium_per_trade: 2000.00
          buying_power_reserve_pct: 20
          no_new_trades_cutoff_utc: "19:30"
          cooldown_after_loss_seconds: 120
        per_strategy:
          max_daily_loss: 2000.00
          max_positions: 3
        per_underlying:
          max_positions: 5
        spread_limits:
          max_spread_pct: 15.0
        quote_freshness:
          max_age_seconds: 5
        kill_switch:
          enabled: true
          trigger_on_daily_loss: true
          trigger_on_disconnect_seconds: 30
    """

    def test_happy_path(self, tmp_path):
        p = _write_yaml(tmp_path, "risk.yaml", self.VALID_RISK_YAML)
        cfg = load_risk_config(p)
        assert cfg.global_.daily_loss_limit == 5000.0
        assert cfg.global_.trading_mode == "paper"
        assert cfg.per_strategy.max_daily_loss == 2000.0
        assert cfg.spread_limits.max_spread_pct == 15.0
        assert cfg.quote_freshness.max_age_seconds == 5.0
        assert cfg.kill_switch.enabled is True

    def test_cutoff_time_property(self, tmp_path):
        from datetime import time

        p = _write_yaml(tmp_path, "risk.yaml", self.VALID_RISK_YAML)
        cfg = load_risk_config(p)
        assert cfg.global_.cutoff_time == time(19, 30)

    def test_empty_risk_config_raises(self, tmp_path):
        p = _write_yaml(tmp_path, "risk.yaml", "# empty\n")
        with pytest.raises(ValueError, match="empty"):
            load_risk_config(p)

    def test_missing_global_section_raises(self, tmp_path):
        p = _write_yaml(tmp_path, "risk.yaml", """\
            per_strategy:
              max_daily_loss: 2000
              max_positions: 3
            per_underlying:
              max_positions: 5
            spread_limits:
              max_spread_pct: 15.0
            quote_freshness:
              max_age_seconds: 5
        """)
        with pytest.raises(ValidationError, match="global"):
            load_risk_config(p)

    def test_negative_daily_loss_limit_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "daily_loss_limit: 5000.00", "daily_loss_limit: -100"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="daily_loss_limit"):
            load_risk_config(p)

    def test_zero_max_open_positions_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "max_open_positions: 10", "max_open_positions: 0"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="max_open_positions"):
            load_risk_config(p)

    def test_zero_max_contracts_per_trade_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "max_contracts_per_trade: 10", "max_contracts_per_trade: 0"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="max_contracts_per_trade"):
            load_risk_config(p)

    def test_zero_max_premium_per_trade_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "max_premium_per_trade: 2000.00", "max_premium_per_trade: 0"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="max_premium_per_trade"):
            load_risk_config(p)

    def test_zero_spread_pct_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "max_spread_pct: 15.0", "max_spread_pct: 0"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="max_spread_pct"):
            load_risk_config(p)

    def test_zero_quote_max_age_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "max_age_seconds: 5", "max_age_seconds: 0"
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="max_age_seconds"):
            load_risk_config(p)

    def test_invalid_cutoff_time_format_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            'no_new_trades_cutoff_utc: "19:30"',
            'no_new_trades_cutoff_utc: "not-a-time"',
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="HH:MM"):
            load_risk_config(p)

    def test_invalid_cutoff_time_hours_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            'no_new_trades_cutoff_utc: "19:30"',
            'no_new_trades_cutoff_utc: "25:00"',
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="invalid time"):
            load_risk_config(p)

    def test_force_flatten_after_cutoff_valid(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "cooldown_after_loss_seconds: 120",
            'force_flatten_time_utc: "19:50"\n          cooldown_after_loss_seconds: 120',
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        cfg = load_risk_config(p)
        assert cfg.global_.force_flatten_time_utc == "19:50"
        from datetime import time
        assert cfg.global_.flatten_time == time(19, 50)

    def test_force_flatten_before_cutoff_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "cooldown_after_loss_seconds: 120",
            'force_flatten_time_utc: "19:00"\n          cooldown_after_loss_seconds: 120',
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(
            ValidationError, match="force_flatten_time_utc.*must be after"
        ):
            load_risk_config(p)

    def test_force_flatten_equals_cutoff_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            "cooldown_after_loss_seconds: 120",
            'force_flatten_time_utc: "19:30"\n          cooldown_after_loss_seconds: 120',
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(
            ValidationError, match="force_flatten_time_utc.*must be after"
        ):
            load_risk_config(p)

    def test_invalid_trading_mode_rejected(self, tmp_path):
        yaml = self.VALID_RISK_YAML.replace(
            'trading_mode: "paper"', 'trading_mode: "yolo"'
        )
        p = _write_yaml(tmp_path, "risk.yaml", yaml)
        with pytest.raises(ValidationError, match="trading_mode"):
            load_risk_config(p)

    def test_missing_per_strategy_section_raises(self, tmp_path):
        p = _write_yaml(tmp_path, "risk.yaml", """\
            global:
              trading_mode: "paper"
              daily_loss_limit: 5000
              max_open_positions: 10
              max_open_orders: 20
              max_contracts_per_trade: 10
              max_premium_per_trade: 2000
              no_new_trades_cutoff_utc: "19:30"
            per_underlying:
              max_positions: 5
            spread_limits:
              max_spread_pct: 15.0
            quote_freshness:
              max_age_seconds: 5
        """)
        with pytest.raises(ValidationError, match="per_strategy"):
            load_risk_config(p)


# ===================================================================
# Strategies config tests
# ===================================================================


class TestStrategiesConfig:
    VALID_STRATEGY_YAML = """\
        strategies:
          - strategy_id: "put_spread_spx"
            enabled: true
            description: "0DTE put spread on SPX"
            underlying: "SPX"
            option_type: "spread"
            direction: "short"
            dte_target: 0
            entry:
              signal_source: "momentum_scanner"
              max_contracts: 5
              limit_price_offset: 0.05
              order_timeout_seconds: 30
            exit:
              stop_loss_pct: 200
              take_profit_pct: 50
              time_exit_utc: "19:45"
            cooldown_seconds: 60
    """

    def test_happy_path(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", self.VALID_STRATEGY_YAML)
        cfg = load_strategies_config(p)
        assert len(cfg.strategies) == 1
        s = cfg.strategies[0]
        assert s.strategy_id == "put_spread_spx"
        assert s.enabled is True
        assert s.entry.max_contracts == 5
        assert s.exit.stop_loss_pct == 200
        assert s.cooldown_seconds == 60

    def test_empty_strategies_file(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", "# no strategies\n")
        cfg = load_strategies_config(p)
        assert cfg.strategies == []

    def test_empty_strategies_list(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", "strategies: []\n")
        cfg = load_strategies_config(p)
        assert cfg.strategies == []

    def test_duplicate_strategy_ids_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "same_id"
                underlying: "SPX"
                entry:
                  signal_source: "scanner_a"
                  max_contracts: 5
              - strategy_id: "same_id"
                underlying: "QQQ"
                entry:
                  signal_source: "scanner_b"
                  max_contracts: 3
        """)
        with pytest.raises(ValidationError, match="Duplicate strategy_id"):
            load_strategies_config(p)

    def test_missing_strategy_id_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - underlying: "SPX"
                entry:
                  signal_source: "scanner"
                  max_contracts: 5
        """)
        with pytest.raises(ValidationError, match="strategy_id"):
            load_strategies_config(p)

    def test_missing_entry_section_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                underlying: "SPX"
        """)
        with pytest.raises(ValidationError, match="entry"):
            load_strategies_config(p)

    def test_missing_underlying_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                entry:
                  signal_source: "scanner"
                  max_contracts: 5
        """)
        with pytest.raises(ValidationError, match="underlying"):
            load_strategies_config(p)

    def test_zero_max_contracts_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                underlying: "SPX"
                entry:
                  signal_source: "scanner"
                  max_contracts: 0
        """)
        with pytest.raises(ValidationError, match="max_contracts"):
            load_strategies_config(p)

    def test_invalid_option_type_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                underlying: "SPX"
                option_type: "butterfly"
                entry:
                  signal_source: "scanner"
                  max_contracts: 5
        """)
        with pytest.raises(ValidationError, match="option_type"):
            load_strategies_config(p)

    def test_invalid_direction_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                underlying: "SPX"
                direction: "neutral"
                entry:
                  signal_source: "scanner"
                  max_contracts: 5
        """)
        with pytest.raises(ValidationError, match="direction"):
            load_strategies_config(p)

    def test_invalid_exit_time_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "test"
                underlying: "SPX"
                entry:
                  signal_source: "scanner"
                  max_contracts: 5
                exit:
                  time_exit_utc: "25:99"
        """)
        with pytest.raises(ValidationError, match="time_exit_utc"):
            load_strategies_config(p)

    def test_multiple_unique_strategies_valid(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "strat_a"
                underlying: "SPX"
                entry:
                  signal_source: "scanner_a"
                  max_contracts: 5
              - strategy_id: "strat_b"
                underlying: "QQQ"
                entry:
                  signal_source: "scanner_b"
                  max_contracts: 3
        """)
        cfg = load_strategies_config(p)
        assert len(cfg.strategies) == 2
        ids = {s.strategy_id for s in cfg.strategies}
        assert ids == {"strat_a", "strat_b"}

    def test_partial_fill_defaults(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", self.VALID_STRATEGY_YAML)
        cfg = load_strategies_config(p)
        s = cfg.strategies[0]
        assert s.partial_fills.entry == "cancel_remainder"
        assert s.partial_fills.exit == "leave_open"

    def test_allow_reentry_defaults_and_explicit(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """
            strategies:
              - strategy_id: "strat_reentry"
                enabled: true
                underlying: "SPX"
                allow_reentry: true
                entry:
                  signal_source: "momentum_scanner"
                  max_contracts: 5
        """)
        cfg = load_strategies_config(p)
        assert len(cfg.strategies) == 1
        assert cfg.strategies[0].allow_reentry is True

        p2 = _write_yaml(tmp_path, "strategies2.yaml", """
            strategies:
              - strategy_id: "strat_default"
                enabled: true
                underlying: "SPX"
                entry:
                  signal_source: "momentum_scanner"
                  max_contracts: 5
        """)
        cfg2 = load_strategies_config(p2)
        assert cfg2.strategies[0].allow_reentry is False


# ===================================================================
# Symbols config tests
# ===================================================================


class TestSymbolsConfig:
    VALID_SYMBOLS_YAML = """\
        symbols:
          - symbol: "SPX"
            exchange: "CBOE"
            security_type: "IND"
            option_exchange: "SMART"
            enabled: true
            max_age_seconds: 5
            trading_hours_utc:
              open: "14:30"
              close: "21:00"
            option_multiplier: 100
    """

    def test_happy_path(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", self.VALID_SYMBOLS_YAML)
        cfg = load_symbols_config(p)
        assert len(cfg.symbols) == 1
        s = cfg.symbols[0]
        assert s.symbol == "SPX"
        assert s.max_age_seconds == 5
        assert s.option_multiplier == 100

    def test_empty_symbols_file(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", "# no symbols\n")
        cfg = load_symbols_config(p)
        assert cfg.symbols == []

    def test_duplicate_symbols_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "SPX"
                exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
              - symbol: "SPX"
                exchange: "SMART"
                security_type: "IND"
                max_age_seconds: 3
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
        """)
        with pytest.raises(ValidationError, match="Duplicate symbol"):
            load_symbols_config(p)

    def test_missing_symbol_name_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
        """)
        with pytest.raises(ValidationError, match="symbol"):
            load_symbols_config(p)

    def test_invalid_security_type_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "AAPL"
                exchange: "SMART"
                security_type: "BOND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
        """)
        with pytest.raises(ValidationError, match="security_type"):
            load_symbols_config(p)

    def test_zero_max_age_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "SPX"
                exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 0
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
        """)
        with pytest.raises(ValidationError, match="max_age_seconds"):
            load_symbols_config(p)

    def test_invalid_trading_hours_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "SPX"
                exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "not-a-time"
                  close: "21:00"
        """)
        with pytest.raises(ValidationError, match="HH:MM"):
            load_symbols_config(p)

    def test_close_before_open_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "SPX"
                exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "21:00"
                  close: "14:30"
        """)
        with pytest.raises(ValidationError, match="close.*must be after open"):
            load_symbols_config(p)

    def test_multiple_symbols_valid(self, tmp_path):
        p = _write_yaml(tmp_path, "symbols.yaml", """\
            symbols:
              - symbol: "SPX"
                exchange: "CBOE"
                security_type: "IND"
                max_age_seconds: 5
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
              - symbol: "QQQ"
                exchange: "SMART"
                security_type: "STK"
                max_age_seconds: 3
                trading_hours_utc:
                  open: "14:30"
                  close: "21:00"
        """)
        cfg = load_symbols_config(p)
        assert len(cfg.symbols) == 2


# ===================================================================
# Overrides config tests
# ===================================================================


class TestOverridesConfig:
    def test_happy_path(self, tmp_path):
        p = _write_yaml(tmp_path, "overrides.yaml", """\
            overrides:
              paused_strategies: ["strat_a"]
              disabled_symbols: ["SPX"]
              reduce_only: true
              system_locked: false
              reduce_only_strategies: []
        """)
        cfg = load_overrides_config(p)
        assert cfg.paused_strategies == ["strat_a"]
        assert cfg.disabled_symbols == ["SPX"]
        assert cfg.reduce_only is True
        assert cfg.system_locked is False

    def test_empty_overrides_defaults(self, tmp_path):
        p = _write_yaml(tmp_path, "overrides.yaml", "# empty\n")
        cfg = load_overrides_config(p)
        assert cfg.paused_strategies == []
        assert cfg.reduce_only is False
        assert cfg.system_locked is False

    def test_flat_format_without_wrapper(self, tmp_path):
        """Config can be flat (no 'overrides' wrapper key)."""
        p = _write_yaml(tmp_path, "overrides.yaml", """\
            paused_strategies: []
            disabled_symbols: ["QQQ"]
            reduce_only: false
            system_locked: true
            reduce_only_strategies: []
        """)
        cfg = load_overrides_config(p)
        assert cfg.system_locked is True
        assert cfg.disabled_symbols == ["QQQ"]


# ===================================================================
# Dashboard config tests
# ===================================================================


class TestDashboardConfig:
    def test_happy_path(self, tmp_path):
        p = _write_yaml(tmp_path, "dashboard.yaml", """\
            dashboard:
              host: "0.0.0.0"
              port: 8080
              refresh_interval_seconds: 5
              panels:
                - positions
                - orders
              auth:
                enabled: true
        """)
        cfg = load_dashboard_config(p)
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8080
        assert cfg.refresh_interval_seconds == 5
        assert len(cfg.panels) == 2
        assert cfg.auth.enabled is True

    def test_empty_dashboard_defaults(self, tmp_path):
        p = _write_yaml(tmp_path, "dashboard.yaml", "# empty\n")
        cfg = load_dashboard_config(p)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8501
        assert len(cfg.panels) == 6

    def test_invalid_port_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "dashboard.yaml", """\
            dashboard:
              port: -1
        """)
        with pytest.raises(ValidationError):
            load_dashboard_config(p)


# ===================================================================
# Loading from actual configs/ directory
# ===================================================================


class TestLoadActualConfigFiles:
    """Smoke tests that verify the real config files in configs/ parse."""

    CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"

    def test_load_actual_broker_config(self):
        cfg = load_broker_config(self.CONFIGS_DIR / "broker.yaml")
        assert cfg.live_trading.enabled is False

    def test_load_actual_risk_config(self):
        cfg = load_risk_config(self.CONFIGS_DIR / "risk.yaml")
        assert cfg.global_.daily_loss_limit > 0

    def test_load_actual_strategies_config(self):
        cfg = load_strategies_config(self.CONFIGS_DIR / "strategies.yaml")
        assert isinstance(cfg.strategies, list)

    def test_load_actual_symbols_config(self):
        cfg = load_symbols_config(self.CONFIGS_DIR / "symbols.yaml")
        assert isinstance(cfg.symbols, list)

    def test_load_actual_overrides_config(self):
        cfg = load_overrides_config(self.CONFIGS_DIR / "overrides.yaml")
        assert cfg.system_locked is False

    def test_load_actual_dashboard_config(self):
        cfg = load_dashboard_config(self.CONFIGS_DIR / "dashboard.yaml")
        assert cfg.port > 0


# ===================================================================
# Cross-cutting acceptance criteria
# ===================================================================


class TestAcceptanceCriteria:
    """Explicit tests for every acceptance criterion in the task spec."""

    def test_malformed_yaml_fails_with_clear_error(self, tmp_path):
        p = _write_yaml(tmp_path, "bad.yaml", "{ broken: [yaml: {!!!\n")
        with pytest.raises(ValueError, match="Malformed YAML"):
            load_broker_config(p)

    def test_live_trading_defaults_false(self):
        """Constructing BrokerConfig with no args defaults enabled=False."""
        cfg = BrokerConfig()
        assert cfg.live_trading.enabled is False

    def test_live_trading_true_requires_explicit_set(self, tmp_path):
        # Default file has enabled: false
        p = _write_yaml(tmp_path, "broker.yaml", """\
            live_trading:
              enabled: false
        """)
        cfg = load_broker_config(p)
        assert cfg.live_trading.enabled is False
        # Must be explicitly set to true
        p2 = _write_yaml(tmp_path, "broker2.yaml", """\
            live_trading:
              enabled: true
        """)
        cfg2 = load_broker_config(p2)
        assert cfg2.live_trading.enabled is True

    def test_duplicate_strategy_ids_rejected(self, tmp_path):
        p = _write_yaml(tmp_path, "strategies.yaml", """\
            strategies:
              - strategy_id: "dup"
                underlying: "SPX"
                entry:
                  signal_source: "s"
                  max_contracts: 1
              - strategy_id: "dup"
                underlying: "QQQ"
                entry:
                  signal_source: "s"
                  max_contracts: 1
        """)
        with pytest.raises(ValidationError, match="Duplicate strategy_id"):
            load_strategies_config(p)

    def test_risk_limits_must_be_positive(self, tmp_path):
        """Every numeric risk limit rejects zero or negative values."""
        base = """\
            global:
              trading_mode: "paper"
              daily_loss_limit: {dll}
              max_open_positions: {mop}
              max_open_orders: {moo}
              max_contracts_per_trade: {mct}
              max_premium_per_trade: {mpt}
              no_new_trades_cutoff_utc: "19:30"
            per_strategy:
              max_daily_loss: {msdl}
              max_positions: {msp}
            per_underlying:
              max_positions: {mup}
            spread_limits:
              max_spread_pct: {mspd}
            quote_freshness:
              max_age_seconds: {mas}
        """

        defaults = {
            "dll": 5000, "mop": 10, "moo": 20, "mct": 10,
            "mpt": 2000, "msdl": 2000, "msp": 3, "mup": 5,
            "mspd": 15, "mas": 5,
        }

        # Test each field with zero
        for key in defaults:
            params = {**defaults, key: 0}
            yaml_str = base.format(**params)
            p = _write_yaml(tmp_path, f"risk_{key}.yaml", yaml_str)
            with pytest.raises(ValidationError):
                load_risk_config(p)

    def test_cutoff_and_flatten_must_be_valid_times(self, tmp_path):
        """Invalid time formats are rejected."""
        base_yaml = """\
            global:
              trading_mode: "paper"
              daily_loss_limit: 5000
              max_open_positions: 10
              max_open_orders: 20
              max_contracts_per_trade: 10
              max_premium_per_trade: 2000
              no_new_trades_cutoff_utc: "{cutoff}"
              force_flatten_time_utc: "{flatten}"
            per_strategy:
              max_daily_loss: 2000
              max_positions: 3
            per_underlying:
              max_positions: 5
            spread_limits:
              max_spread_pct: 15.0
            quote_freshness:
              max_age_seconds: 5
        """
        # Invalid cutoff
        p1 = _write_yaml(
            tmp_path, "risk_bad_cutoff.yaml",
            base_yaml.format(cutoff="invalid", flatten="19:50"),
        )
        with pytest.raises(ValidationError):
            load_risk_config(p1)

        # Invalid flatten
        p2 = _write_yaml(
            tmp_path, "risk_bad_flatten.yaml",
            base_yaml.format(cutoff="19:30", flatten="bad"),
        )
        with pytest.raises(ValidationError):
            load_risk_config(p2)

    def test_force_flatten_must_be_after_cutoff(self, tmp_path):
        p = _write_yaml(tmp_path, "risk.yaml", """\
            global:
              trading_mode: "paper"
              daily_loss_limit: 5000
              max_open_positions: 10
              max_open_orders: 20
              max_contracts_per_trade: 10
              max_premium_per_trade: 2000
              no_new_trades_cutoff_utc: "19:30"
              force_flatten_time_utc: "19:00"
            per_strategy:
              max_daily_loss: 2000
              max_positions: 3
            per_underlying:
              max_positions: 5
            spread_limits:
              max_spread_pct: 15.0
            quote_freshness:
              max_age_seconds: 5
        """)
        with pytest.raises(ValidationError, match="must be after"):
            load_risk_config(p)

    def test_missing_required_fields_fail_loudly(self, tmp_path):
        """Missing required fields produce clear ValidationError, not None."""
        # Missing 'global' section entirely
        p = _write_yaml(tmp_path, "risk.yaml", """\
            per_strategy:
              max_daily_loss: 2000
              max_positions: 3
            per_underlying:
              max_positions: 5
            spread_limits:
              max_spread_pct: 15.0
            quote_freshness:
              max_age_seconds: 5
        """)
        with pytest.raises(ValidationError) as exc_info:
            load_risk_config(p)
        # Error message should mention the missing field
        assert "global" in str(exc_info.value)
