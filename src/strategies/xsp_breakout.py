import logging
from datetime import datetime, UTC, date, time
from zoneinfo import ZoneInfo
from typing import Optional, Any
from uuid import UUID, uuid4

from src.app.runner import StrategyProvider
from src.core.config import StrategyConfig
from src.core.enums import SignalDirection, OptionRight
from src.core.models import StrategySignal, OptionContract
from src.portfolio.position_manager import PositionManager
from src.broker.interface import BrokerClient

logger = logging.getLogger(__name__)

STRATEGY_PARAMS = {
    "xsp_0dte_1000": {"hour": 10, "minute": 0, "trigger_pct": 0.002},
    "xsp_0dte_1030": {"hour": 10, "minute": 30, "trigger_pct": 0.002},
    "xsp_0dte_1100": {"hour": 11, "minute": 0, "trigger_pct": 0.003},
    "xsp_0dte_1200": {"hour": 12, "minute": 0, "trigger_pct": 0.002},
    "xsp_0dte_1230": {"hour": 12, "minute": 30, "trigger_pct": 0.003},
}

class XSPBreakoutStrategyProvider(StrategyProvider):
    """Breakout momentum strategy on XSP 0DTE options."""

    # Subclasses override this to define their own entry windows.
    STRATEGY_PARAMS = STRATEGY_PARAMS

    def __init__(self, broker: BrokerClient, position_manager: Optional[PositionManager] = None) -> None:
        self.broker = broker
        self._position_manager = position_manager
        
        # Cache for 9:30 AM close reference price by date
        self._ref_prices: dict[date, float] = {}
        
        # Set of dates where historical data fetch failed/returned None to prevent spamming
        self._failed_fetches: set[date] = set()
        
        # Track generated signals/trades in memory for today
        self._traded_today: set[tuple[date, str]] = set()
        
        # For unit testing
        self.mock_reference_price: Optional[float] = None

    def set_position_manager(self, position_manager: PositionManager) -> None:
        self._position_manager = position_manager

    def _reference_bar_time(self, current_date: date, tz_ny) -> datetime:
        """Return the datetime of the reference bar close. Override in subclasses."""
        # 9:30 close bar ends at 9:31:00 AM NY time
        return datetime.combine(current_date, time(9, 31), tzinfo=tz_ny)

    @property
    def _reference_label(self) -> str:
        """Human-readable label for the reference price. Override in subclasses."""
        return "9:30 AM"

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Poll strategy for signal at the current tick."""
        # 1. ZoneInfo conversion to New York
        tz_ny = ZoneInfo("America/New_York")
        time_ny = current_time.astimezone(tz_ny)
        current_date = time_ny.date()

        # 2. Resolve entry params: config (dashboard-editable) overrides
        # the legacy code-side STRATEGY_PARAMS table.
        code_params = self.STRATEGY_PARAMS.get(strategy_config.strategy_id, {})
        cfg_entry_time = getattr(strategy_config.entry, "entry_time", None)
        cfg_trigger = getattr(strategy_config.entry, "trigger_pct", None)
        params = {}
        if cfg_entry_time:
            try:
                hh, mm = cfg_entry_time.split(":")
                params["hour"], params["minute"] = int(hh), int(mm)
            except ValueError:
                logger.error("Invalid entry_time %r for %s", cfg_entry_time, strategy_config.strategy_id)
        if "hour" not in params and code_params:
            params["hour"], params["minute"] = code_params["hour"], code_params["minute"]
        params["trigger_pct"] = cfg_trigger if cfg_trigger is not None else code_params.get("trigger_pct")
        if "hour" not in params or params["trigger_pct"] is None:
            logger.warning("Strategy ID %s has no entry params (config or code table)", strategy_config.strategy_id)
            return []

        # 3. Check if current NY time is before the scan start time
        scan_time_ny = datetime.combine(current_date, time(params["hour"], params["minute"]), tzinfo=tz_ny)
        if time_ny < scan_time_ny:
            return []

        # 4. Enforce max 1 trade/signal per system per day
        # Check in-memory trade tracker
        allow_reentry = getattr(strategy_config, "allow_reentry", False)
        if not allow_reentry:
            if (current_date, strategy_config.strategy_id) in self._traded_today:
                return []

            # Check PositionManager for past trades today (e.g. after a restart)
            if self._position_manager:
                for pos in self._position_manager.positions.values():
                    if pos.strategy_id == strategy_config.strategy_id:
                        pos_time = pos.entry_time or pos.created_at
                        pos_time_ny = pos_time.astimezone(tz_ny)
                        if pos_time_ny.date() == current_date:
                            return []

        # 5. Fetch/verify 9:30 AM reference price
        ref_price = self._ref_prices.get(current_date)
        if ref_price is None:
            if current_date in self._failed_fetches:
                return []
                
            if self.mock_reference_price is not None:
                self._ref_prices[current_date] = self.mock_reference_price
                ref_price = self.mock_reference_price
            else:
                ref_time_ny = self._reference_bar_time(current_date, tz_ny)
                try:
                    price = await self.broker.get_historical_close("XSP", ref_time_ny)
                    if price is not None:
                        self._ref_prices[current_date] = price
                        ref_price = price
                        logger.info("Cached XSP %s reference price: %s for %s", self._reference_label, price, current_date)
                    else:
                        logger.warning("Failed to fetch historical %s close price for XSP on %s. Skipping strategy %s today.", self._reference_label, current_date, strategy_config.strategy_id)
                        self._failed_fetches.add(current_date)
                        return []
                except Exception as e:
                    logger.error("Error fetching historical %s close price for XSP on %s: %s. Skipping strategy %s today.", self._reference_label, current_date, e, strategy_config.strategy_id)
                    self._failed_fetches.add(current_date)
                    return []

        # 6. Fetch current underlying price
        try:
            quotes = await self.broker.get_quotes(["XSP"])
            quote = quotes.get("XSP")
        except Exception as e:
            logger.error("Error fetching quote for XSP: %s", e)
            return []

        if not quote:
            logger.debug("Stale or missing quote for XSP: %s", quote)
            return []

        # Backtest fidelity: the breakout trigger is evaluated on 1-minute
        # bar CLOSES of the underlying (pct_from_open on bar closes), not on
        # instantaneous quotes. Prefer the latest completed 1-min bar close;
        # fall back to the live quote when no bar data exists yet.
        current_underlying_price = None
        price_source = "quote"
        try:
            bar = self.broker.get_latest_completed_bar("XSP")
        except Exception:
            bar = None
        if bar is not None:
            current_underlying_price = bar.close
            price_source = "bar_close"
        elif quote.bid is not None and quote.ask is not None:
            current_underlying_price = (quote.bid + quote.ask) / 2.0
        elif quote.last is not None:
            current_underlying_price = quote.last
        elif quote.close is not None:
            current_underlying_price = quote.close
        else:
            logger.debug("Stale or missing price for XSP: %s", quote)
            return []

        # 7. Check trigger condition
        price_change = (current_underlying_price - ref_price) / ref_price
        trigger_pct = params["trigger_pct"]

        right = None
        trigger_type = None

        if price_change >= trigger_pct:
            right = OptionRight.CALL
            trigger_type = "breakout_long"
        elif price_change <= -trigger_pct:
            right = OptionRight.PUT
            trigger_type = "breakout_short"
        else:
            return []

        # 8. Create Contract and Signal
        strike = float(round(current_underlying_price))
        expiry_str = current_date.strftime("%Y%m%d")
        
        contract = OptionContract(
            symbol="XSP",
            expiry=expiry_str,
            strike=strike,
            right=right,
            multiplier=100
        )

        # 9. Dynamic position sizing (matches backtest):
        #    qty = max(1, int(equity * position_sizing_pct / (option_price * 100)))
        #    capped by entry.max_contracts as a safety limit.
        position_sizing_pct = getattr(strategy_config, "position_sizing_pct", None) or 0.01

        option_mid: Optional[float] = None
        try:
            opt_quotes = await self.broker.get_quotes([contract.to_quote_symbol()])
            opt_quote = opt_quotes.get(contract.to_quote_symbol())
            if opt_quote:
                if opt_quote.bid is not None and opt_quote.ask is not None:
                    option_mid = (opt_quote.bid + opt_quote.ask) / 2.0
                elif opt_quote.last is not None:
                    option_mid = opt_quote.last
        except Exception as e:
            logger.warning("Failed to fetch option quote for sizing (%s): %s", contract.to_quote_symbol(), e)

        available_cap: Optional[float] = None
        try:
            account_state = await self.broker.get_account_state()
            available_cap = account_state.net_liquidation
        except Exception as e:
            logger.warning("Failed to fetch account state for sizing: %s", e)

        if option_mid is not None and option_mid > 0 and available_cap is not None and available_cap > 0:
            qty = max(1, int(available_cap * position_sizing_pct / (option_mid * 100.0)))
            qty = min(qty, strategy_config.entry.max_contracts)
        else:
            # Fail safe: minimum size if pricing/account data is unavailable
            logger.warning(
                "Sizing inputs unavailable (mid=%s, netliq=%s) for %s; defaulting to qty=1",
                option_mid, available_cap, strategy_config.strategy_id,
            )
            qty = 1

        metadata = {
            "trigger_type": trigger_type,
            "reference_price": ref_price,
            "underlying_price_at_entry": current_underlying_price,
            "atm_strike": strike,
            "option_mid_at_signal": option_mid,
            "net_liquidation": available_cap,
            "position_sizing_pct": position_sizing_pct,
        }

        # Record signal emitted to avoid duplicates
        self._traded_today.add((current_date, strategy_config.strategy_id))

        signal = StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.LONG,
            contract=contract,
            requested_quantity=qty,
            limit_price=round(option_mid, 2) if option_mid else None,
            timestamp=current_time,
            metadata=metadata
        )

        logger.info(
            "Emitted breakout signal for %s: %s strike=%s, ref=%s, current=%s, change=%s%%",
            strategy_config.strategy_id,
            right,
            strike,
            ref_price,
            current_underlying_price,
            round(price_change * 100, 4)
        )

        return [signal]
