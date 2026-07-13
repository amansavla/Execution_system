"""Config loading and validation for all YAML configuration files.

Each config file has a corresponding Pydantic v2 model that validates
the YAML structure at load time. Malformed YAML or missing required
fields produce clear, actionable error messages.

No broker-specific imports or logic.

Loader functions:
    load_broker_config(path)      -> BrokerConfig
    load_risk_config(path)        -> FullRiskConfig
    load_strategies_config(path)  -> StrategiesConfig
    load_symbols_config(path)     -> SymbolsConfig
    load_overrides_config(path)   -> OverridesConfig
    load_dashboard_config(path)   -> DashboardConfig
"""

from __future__ import annotations

from datetime import time
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ===================================================================
# Shared helpers
# ===================================================================

class _TimeStr(str):
    """Marker type — validated as HH:MM time string."""


def _parse_hhmm(value: str, field_name: str) -> time:
    """Parse an 'HH:MM' string into a datetime.time.

    Raises ValueError with a clear message on failure.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string in HH:MM format, got {type(value).__name__}")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be in HH:MM format, got '{value}'")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"{field_name} must be in HH:MM format, got '{value}'")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{field_name} has invalid time components: hour={h}, minute={m}")
    return time(h, m)


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return its contents as a dict.

    Raises FileNotFoundError if the file does not exist.
    Raises ValueError if the file is not valid YAML or is empty.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Malformed YAML in {path}: {e}") from e
    if data is None:
        # File exists but is empty or all-comments
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}, got {type(data).__name__}")
    return data


# ===================================================================
# Broker config — configs/broker.yaml
# ===================================================================

class ConnectionConfig(BaseModel):
    """IBKR connection settings."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=7497, ge=1, le=65535)
    client_id: int = Field(default=1, ge=0)
    timeout_seconds: int = Field(default=30, gt=0)
    readonly: bool = False


class AccountConfig(BaseModel):
    """Account identification and allowlist."""

    account_id: str = Field(default="")
    allowlist: list[str] = Field(default_factory=list)


class ReconnectionConfig(BaseModel):
    """Reconnection behavior on broker disconnect."""

    enabled: bool = True
    max_retries: int = Field(default=5, ge=0)
    retry_delay_seconds: int = Field(default=10, gt=0)
    stale_on_disconnect: bool = True


class OrderDefaultsConfig(BaseModel):
    """Default order parameters."""

    order_type: str = Field(default="LMT")
    time_in_force: str = Field(default="DAY")
    transmit: bool = True
    # IBKR Adaptive algo priority for single-leg limit orders
    # ("Urgent" | "Normal" | "Patient" | null to disable). Applied only
    # during regular trading hours — IBKR algos are RTH-only.
    adaptive_priority: Optional[str] = Field(default="Urgent")


class LiveTradingConfig(BaseModel):
    """Live trading toggle — defaults to disabled."""

    enabled: bool = Field(default=False)


class BrokerConfig(BaseModel):
    """Top-level broker configuration loaded from configs/broker.yaml."""

    live_trading: LiveTradingConfig = Field(default_factory=LiveTradingConfig)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    reconnection: ReconnectionConfig = Field(default_factory=ReconnectionConfig)
    order_defaults: OrderDefaultsConfig = Field(default_factory=OrderDefaultsConfig)


def load_broker_config(path: Path) -> BrokerConfig:
    """Load and validate configs/broker.yaml."""
    data = _load_yaml(path)
    return BrokerConfig.model_validate(data)


# ===================================================================
# Risk config — configs/risk.yaml
# ===================================================================

class KillSwitchConfig(BaseModel):
    """Kill switch settings."""

    enabled: bool = True
    trigger_on_daily_loss: bool = True
    trigger_on_disconnect_seconds: int = Field(default=30, gt=0)


class SpreadLimitsConfig(BaseModel):
    """Bid-ask spread limits."""

    max_spread_pct: float = Field(..., gt=0)


class QuoteFreshnessConfig(BaseModel):
    """Quote staleness thresholds.

    max_age_seconds gates ENTRIES (RiskEngine). Exits use the wider
    exit_max_age_seconds: streaming option tickers only refresh their
    timestamp when the NBBO changes, so a quiet contract looks 'stale'
    within seconds — and skipping stop-loss evaluation on a live position
    is far riskier than evaluating an unchanged NBBO (2026-06-11: stop
    checks were skipped for 10s+ stretches mid-session).
    """

    max_age_seconds: float = Field(..., gt=0)
    exit_max_age_seconds: float = Field(default=30.0, gt=0)


class PerStrategyRiskConfig(BaseModel):
    """Per-strategy risk limits."""

    max_daily_loss: float = Field(..., gt=0)
    max_positions: int = Field(..., gt=0)


class PerUnderlyingRiskConfig(BaseModel):
    """Per-underlying risk limits."""

    max_positions: int = Field(..., gt=0)


class GlobalRiskConfig(BaseModel):
    """Global risk parameters."""

    trading_mode: str = Field(default="paper", pattern=r"^(paper|live|disabled)$")
    daily_loss_limit: float = Field(..., gt=0)
    max_open_positions: int = Field(..., gt=0)
    max_open_orders: int = Field(..., gt=0)
    max_contracts_per_trade: int = Field(..., gt=0)
    max_premium_per_trade: float = Field(..., gt=0)
    buying_power_reserve_pct: float = Field(default=20.0, ge=0, le=100)
    no_new_trades_cutoff_utc: str = Field(...)
    force_flatten_time_utc: Optional[str] = Field(default=None)
    cooldown_after_loss_seconds: int = Field(default=120, ge=0)

    # Parsed time objects (populated by validator)
    _parsed_cutoff: Optional[time] = None
    _parsed_flatten: Optional[time] = None

    @field_validator("no_new_trades_cutoff_utc")
    @classmethod
    def _validate_cutoff_time(cls, v: str) -> str:
        _parse_hhmm(v, "no_new_trades_cutoff_utc")
        return v

    @field_validator("force_flatten_time_utc")
    @classmethod
    def _validate_flatten_time(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _parse_hhmm(v, "force_flatten_time_utc")
        return v

    @model_validator(mode="after")
    def _flatten_after_cutoff(self) -> "GlobalRiskConfig":
        if self.force_flatten_time_utc is not None:
            cutoff = _parse_hhmm(self.no_new_trades_cutoff_utc, "no_new_trades_cutoff_utc")
            flatten = _parse_hhmm(self.force_flatten_time_utc, "force_flatten_time_utc")
            if flatten <= cutoff:
                raise ValueError(
                    f"force_flatten_time_utc ({self.force_flatten_time_utc}) "
                    f"must be after no_new_trades_cutoff_utc ({self.no_new_trades_cutoff_utc})"
                )
        return self

    @property
    def cutoff_time(self) -> time:
        """Parsed no_new_trades_cutoff as datetime.time."""
        return _parse_hhmm(self.no_new_trades_cutoff_utc, "no_new_trades_cutoff_utc")

    @property
    def flatten_time(self) -> Optional[time]:
        """Parsed force_flatten_time as datetime.time, or None."""
        if self.force_flatten_time_utc is None:
            return None
        return _parse_hhmm(self.force_flatten_time_utc, "force_flatten_time_utc")


class FullRiskConfig(BaseModel):
    """Top-level risk configuration loaded from configs/risk.yaml."""

    global_: GlobalRiskConfig = Field(..., alias="global")
    per_strategy: PerStrategyRiskConfig
    per_underlying: PerUnderlyingRiskConfig
    spread_limits: SpreadLimitsConfig
    quote_freshness: QuoteFreshnessConfig
    kill_switch: KillSwitchConfig = Field(default_factory=KillSwitchConfig)

    model_config = {"populate_by_name": True}


def load_risk_config(path: Path) -> FullRiskConfig:
    """Load and validate configs/risk.yaml."""
    data = _load_yaml(path)
    if not data:
        raise ValueError(f"Risk config at {path} is empty — all fields are required")
    return FullRiskConfig.model_validate(data)


# ===================================================================
# Strategies config — configs/strategies.yaml
# ===================================================================

class PartialFillBehavior(str, Enum):
    """What to do with remainder after partial fill."""

    CANCEL_REMAINDER = "cancel_remainder"
    LEAVE_OPEN = "leave_open"


class StrategyEntryConfig(BaseModel):
    """Entry parameters for a strategy."""

    signal_source: str = Field(..., min_length=1)
    max_contracts: int = Field(..., gt=0)
    limit_price_offset: float = Field(default=0.05, ge=0)
    order_timeout_seconds: int = Field(default=30, gt=0)
    # Optional config-side entry params (override code defaults; editable
    # from the dashboard). entry_time is NY local "HH:MM".
    entry_time: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    trigger_pct: Optional[float] = Field(default=None, gt=0)
    # Strike-selection mode for strategies that scan a chain (e.g. the short
    # straddle): "premium_target" picks the strike whose mid is closest to a
    # computed target premium; "delta_target" picks the strike whose |delta|
    # is closest to target_delta instead. target_delta is a magnitude
    # (0.30 matches call delta +0.30 and put delta -0.30).
    strike_selection: Literal["premium_target", "delta_target"] = Field(default="premium_target")
    target_delta: Optional[float] = Field(default=None, gt=0, lt=1)


class StrategyExitConfig(BaseModel):
    """Exit parameters (ExitManager defaults)."""

    stop_loss_pct: Optional[float] = Field(default=None, gt=0)
    take_profit_pct: Optional[float] = Field(default=None, gt=0)
    time_exit_utc: Optional[str] = None
    # Relative time exit: close N seconds after entry. Takes precedence
    # over time_exit_utc when both are set. Used by cycling test strategies.
    max_hold_seconds: Optional[int] = Field(default=None, gt=0)
    trailing_stop: bool = False
    use_mid_for_exits: bool = False

    @field_validator("time_exit_utc")
    @classmethod
    def _validate_time_exit(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _parse_hhmm(v, "time_exit_utc")
        return v


class StrategyPartialFillConfig(BaseModel):
    """Partial fill behavior overrides."""

    entry: PartialFillBehavior = PartialFillBehavior.CANCEL_REMAINDER
    exit: PartialFillBehavior = PartialFillBehavior.LEAVE_OPEN


class StrategyConfig(BaseModel):
    """Configuration for a single strategy."""

    strategy_id: str = Field(..., min_length=1)
    enabled: bool = True
    description: str = Field(default="")
    underlying: str = Field(..., min_length=1)
    option_type: str = Field(default="single", pattern=r"^(spread|single)$")
    direction: str = Field(default="long", pattern=r"^(long|short)$")
    dte_target: int = Field(default=0, ge=0, le=1)
    entry: StrategyEntryConfig
    exit: StrategyExitConfig = Field(default_factory=StrategyExitConfig)
    partial_fills: StrategyPartialFillConfig = Field(
        default_factory=StrategyPartialFillConfig
    )
    cooldown_seconds: int = Field(default=60, ge=0)
    leverage: Optional[float] = Field(default=12.0, gt=0)
    position_sizing_pct: Optional[float] = Field(default=0.025, gt=0)
    allow_reentry: bool = False


class StrategiesConfig(BaseModel):
    """Top-level strategies configuration loaded from configs/strategies.yaml.

    Enforces unique strategy IDs across all strategies.
    """

    strategies: list[StrategyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_strategy_ids(self) -> "StrategiesConfig":
        seen: set[str] = set()
        duplicates: list[str] = []
        for s in self.strategies:
            if s.strategy_id in seen:
                duplicates.append(s.strategy_id)
            seen.add(s.strategy_id)
        if duplicates:
            raise ValueError(
                f"Duplicate strategy_id(s) found: {duplicates}. "
                "Each strategy must have a unique strategy_id."
            )
        return self


def load_strategies_config(path: Path) -> StrategiesConfig:
    """Load and validate configs/strategies.yaml."""
    data = _load_yaml(path)
    if not data:
        # Empty file = no strategies configured (valid, just empty)
        return StrategiesConfig(strategies=[])
    return StrategiesConfig.model_validate(data)


# ===================================================================
# Symbols config — configs/symbols.yaml
# ===================================================================

class TradingHoursConfig(BaseModel):
    """Market hours in UTC."""

    open: str = Field(...)
    close: str = Field(...)

    @field_validator("open", "close")
    @classmethod
    def _validate_time(cls, v: str, info) -> str:
        _parse_hhmm(v, info.field_name)
        return v

    @model_validator(mode="after")
    def _open_before_close(self) -> "TradingHoursConfig":
        open_t = _parse_hhmm(self.open, "open")
        close_t = _parse_hhmm(self.close, "close")
        if close_t <= open_t:
            raise ValueError(
                f"Trading hours close ({self.close}) must be after open ({self.open})"
            )
        return self


class SymbolConfig(BaseModel):
    """Configuration for a single tradeable underlying."""

    symbol: str = Field(..., min_length=1)
    exchange: str = Field(..., min_length=1)
    security_type: str = Field(..., pattern=r"^(IND|STK|ETF)$")
    option_exchange: str = Field(default="SMART", min_length=1)
    enabled: bool = True
    max_age_seconds: float = Field(..., gt=0)
    trading_hours_utc: TradingHoursConfig
    option_multiplier: int = Field(default=100, gt=0)


class SymbolsConfig(BaseModel):
    """Top-level symbols configuration loaded from configs/symbols.yaml.

    Enforces unique symbol names.
    """

    symbols: list[SymbolConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_symbols(self) -> "SymbolsConfig":
        seen: set[str] = set()
        duplicates: list[str] = []
        for s in self.symbols:
            if s.symbol in seen:
                duplicates.append(s.symbol)
            seen.add(s.symbol)
        if duplicates:
            raise ValueError(f"Duplicate symbol(s) found: {duplicates}")
        return self


def load_symbols_config(path: Path) -> SymbolsConfig:
    """Load and validate configs/symbols.yaml."""
    data = _load_yaml(path)
    if not data:
        return SymbolsConfig(symbols=[])
    return SymbolsConfig.model_validate(data)


# ===================================================================
# Overrides config — configs/overrides.yaml
# ===================================================================

class OverridesConfig(BaseModel):
    """Top-level overrides loaded from configs/overrides.yaml."""

    paused_strategies: list[str] = Field(default_factory=list)
    disabled_symbols: list[str] = Field(default_factory=list)
    reduce_only: bool = False
    system_locked: bool = False
    reduce_only_strategies: list[str] = Field(default_factory=list)


def load_overrides_config(path: Path) -> OverridesConfig:
    """Load and validate configs/overrides.yaml."""
    data = _load_yaml(path)
    if not data:
        return OverridesConfig()
    # The YAML may have a top-level "overrides" key or be flat
    if "overrides" in data and isinstance(data["overrides"], dict):
        return OverridesConfig.model_validate(data["overrides"])
    return OverridesConfig.model_validate(data)


# ===================================================================
# Dashboard config — configs/dashboard.yaml
# ===================================================================

class DashboardAuthConfig(BaseModel):
    """Dashboard authentication settings."""

    enabled: bool = False
    password_hash: str = Field(default="")


class DashboardConfig(BaseModel):
    """Top-level dashboard configuration loaded from configs/dashboard.yaml."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8501, ge=1, le=65535)
    refresh_interval_seconds: int = Field(default=2, gt=0)
    panels: list[str] = Field(
        default_factory=lambda: [
            "positions", "orders", "risk_status",
            "pnl_summary", "event_log", "manual_controls",
        ]
    )
    auth: DashboardAuthConfig = Field(default_factory=DashboardAuthConfig)


def load_dashboard_config(path: Path) -> DashboardConfig:
    """Load and validate configs/dashboard.yaml."""
    data = _load_yaml(path)
    if not data:
        return DashboardConfig()
    if "dashboard" in data and isinstance(data["dashboard"], dict):
        return DashboardConfig.model_validate(data["dashboard"])
    return DashboardConfig.model_validate(data)
