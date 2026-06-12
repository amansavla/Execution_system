import logging
import re
from datetime import datetime, UTC, date, time
from zoneinfo import ZoneInfo
from typing import Optional

from src.app.runner import StrategyProvider
from src.core.config import StrategyConfig
from src.core.enums import SignalDirection, OptionRight, PositionStatus
from src.core.models import StrategySignal, OptionContract
from src.portfolio.position_manager import PositionManager
from src.broker.interface import BrokerClient

logger = logging.getLogger(__name__)


def parse_entry_time(strategy_id: str) -> Optional[tuple[int, int]]:
    """Parse hour and minute from strategy ID (e.g. 'xsp_straddle_1000_20' -> (10, 0))."""
    m = re.search(r"xsp_straddle_(\d{2})(\d{2})", strategy_id)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


class XSPShortStraddleStrategyProvider(StrategyProvider):
    """ATM Short Straddle strategy on XSP 0DTE options with per-leg independent stop-loss."""

    def __init__(self, broker: BrokerClient, position_manager: Optional[PositionManager] = None) -> None:
        self.broker = broker
        self._position_manager = position_manager
        
        # Track generated signals/trades in memory for today
        self._traded_today: set[tuple[date, str]] = set()
        # strategy_id -> unix time of last "no quotes" warning (rate limit)
        self._no_quote_warned_at: dict[str, float] = {}

    def set_position_manager(self, position_manager: PositionManager) -> None:
        self._position_manager = position_manager

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Poll strategy for signal at the current tick."""
        # 1. ZoneInfo conversion to New York
        tz_ny = ZoneInfo("America/New_York")
        time_ny = current_time.astimezone(tz_ny)
        current_date = time_ny.date()

        # 2. Entry time: config (dashboard-editable) overrides strategy-ID parse
        cfg_entry_time = getattr(strategy_config.entry, "entry_time", None)
        if cfg_entry_time:
            try:
                hh, mm = cfg_entry_time.split(":")
                entry_time_parsed = (int(hh), int(mm))
            except ValueError:
                entry_time_parsed = parse_entry_time(strategy_config.strategy_id)
        else:
            entry_time_parsed = parse_entry_time(strategy_config.strategy_id)
        if not entry_time_parsed:
            logger.warning(
                "Strategy ID %s does not match expected short straddle naming format (xsp_straddle_HHMM). Skipping.",
                strategy_config.strategy_id
            )
            return []

        hour, minute = entry_time_parsed
        entry_time_ny = datetime.combine(current_date, time(hour, minute), tzinfo=tz_ny)

        # 3. Check if current NY time is before scan time
        if time_ny < entry_time_ny:
            return []

        # 4. Enforce max 1 trade/signal per strategy per day
        allow_reentry = getattr(strategy_config, "allow_reentry", False)
        if not allow_reentry:
            if (current_date, strategy_config.strategy_id) in self._traded_today:
                return []

            # Check PositionManager for past trades today (after restart)
            if self._position_manager:
                for pos in self._position_manager.positions.values():
                    if pos.strategy_id == strategy_config.strategy_id:
                        pos_time = pos.entry_time or pos.created_at
                        pos_time_ny = pos_time.astimezone(tz_ny)
                        if pos_time_ny.date() == current_date:
                            return []

        # 5. Fetch current underlying price
        try:
            quotes = await self.broker.get_quotes(["XSP"])
            quote = quotes.get("XSP")
        except Exception as e:
            logger.error("Error fetching quote for XSP: %s", e)
            return []

        if not quote:
            logger.debug("Stale or missing quote for XSP: %s", quote)
            return []

        # For index underlyings, bid/ask are typically None. Use last or close as fallback.
        if quote.bid is not None and quote.ask is not None:
            current_underlying_price = (quote.bid + quote.ask) / 2.0
        elif quote.last is not None:
            current_underlying_price = quote.last
        elif quote.close is not None:
            current_underlying_price = quote.close
        else:
            logger.debug("Stale or missing price for XSP: %s", quote)
            return []

        # 6. Calculate target premium
        leverage = strategy_config.leverage or 12.0
        position_sizing_pct = strategy_config.position_sizing_pct or 0.025
        
        # margin = (undl * multiplier * 2) / leverage
        # target_premium = (margin * position_sizing_pct) / multiplier
        # Simplifying: target_premium = (undl * 2 * position_sizing_pct) / leverage
        target_premium = (current_underlying_price * 2.0 * position_sizing_pct) / leverage

        # 7. Construct candidate strikes (±12 strikes around ATM)
        atm_strike = round(current_underlying_price)
        strike_range = range(atm_strike - 12, atm_strike + 13)
        expiry_str = current_date.strftime("%Y%m%d")

        call_candidates: list[OptionContract] = []
        put_candidates: list[OptionContract] = []

        for strike in strike_range:
            call_candidates.append(
                OptionContract(
                    symbol="XSP",
                    expiry=expiry_str,
                    strike=float(strike),
                    right=OptionRight.CALL,
                    multiplier=100
                )
            )
            put_candidates.append(
                OptionContract(
                    symbol="XSP",
                    expiry=expiry_str,
                    strike=float(strike),
                    right=OptionRight.PUT,
                    multiplier=100
                )
            )

        # 8. Query option quotes
        call_symbols = {c.to_quote_symbol(): c for c in call_candidates}
        put_symbols = {p.to_quote_symbol(): p for p in put_candidates}
        all_symbols = list(call_symbols.keys()) + list(put_symbols.keys())

        try:
            option_quotes = await self.broker.get_quotes(all_symbols)
        except Exception as e:
            logger.error("Error fetching option quotes for straddle strikes: %s", e)
            return []

        # 9. Find best Call strike (closest to target premium)
        best_call: Optional[OptionContract] = None
        best_call_price: Optional[float] = None
        best_call_diff = float("inf")

        for sym, contract in call_symbols.items():
            q = option_quotes.get(sym)
            if not q or q.bid is None or q.ask is None:
                continue
            mid = (q.bid + q.ask) / 2.0
            diff = abs(mid - target_premium)
            if diff < best_call_diff:
                best_call_diff = diff
                best_call = contract
                best_call_price = mid

        # 10. Find best Put strike (closest to target premium)
        best_put: Optional[OptionContract] = None
        best_put_price: Optional[float] = None
        best_put_diff = float("inf")

        for sym, contract in put_symbols.items():
            q = option_quotes.get(sym)
            if not q or q.bid is None or q.ask is None:
                continue
            mid = (q.bid + q.ask) / 2.0
            diff = abs(mid - target_premium)
            if diff < best_put_diff:
                best_put_diff = diff
                best_put = contract
                best_put_price = mid

        if not best_call or not best_put:
            # Rate-limited: this fires every tick when the option chain has
            # no quotes (e.g. after hours) and was flooding the log.
            now_mono = current_time.timestamp()
            last = self._no_quote_warned_at.get(strategy_config.strategy_id, 0.0)
            if now_mono - last >= 60.0:
                self._no_quote_warned_at[strategy_config.strategy_id] = now_mono
                logger.warning(
                    "Could not find valid Call/Put quotes for straddle strategy %s. "
                    "Skipping (suppressing repeats for 60s).",
                    strategy_config.strategy_id
                )
            return []

        # 11. Sizing Calculation
        try:
            account_state = await self.broker.get_account_state()
            available_cap = account_state.net_liquidation
        except Exception as e:
            logger.warning("Failed to fetch account state: %s. Using default capital 100k.", e)
            available_cap = 100000.0

        # 11b. Sizing — exact backtest formula (per-leg, premium-based):
        #   margin     = underlying * multiplier * 2 / leverage
        #   upside_leg = (leg_entry_price * multiplier) / margin
        #   ps_leg     = min(upside_leg, position_sizing_pct)
        #   qty_leg    = max(1, int(equity * ps_leg / (leg_entry_price * multiplier)))
        margin = (current_underlying_price * 100.0 * 2.0) / leverage

        upside_call = (best_call_price * 100.0) / margin
        ps_call = min(upside_call, position_sizing_pct)
        qty_call = max(1, int((available_cap * ps_call) / (best_call_price * 100.0)))
        qty_call = min(qty_call, strategy_config.entry.max_contracts)

        upside_put = (best_put_price * 100.0) / margin
        ps_put = min(upside_put, position_sizing_pct)
        qty_put = max(1, int((available_cap * ps_put) / (best_put_price * 100.0)))
        qty_put = min(qty_put, strategy_config.entry.max_contracts)

        # 12. Create Signals
        metadata = {
            "underlying_price_at_entry": current_underlying_price,
            "target_premium": target_premium,
            "leverage": leverage,
            "position_sizing_pct": position_sizing_pct,
            "available_capital": available_cap,
            "margin_per_leg": margin,
        }

        call_signal = StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.SHORT,
            contract=best_call,
            requested_quantity=qty_call,
            limit_price=round(best_call_price, 2),
            timestamp=current_time,
            metadata={**metadata, "leg": "call", "option_mid": best_call_price}
        )

        put_signal = StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.SHORT,
            contract=best_put,
            requested_quantity=qty_put,
            limit_price=round(best_put_price, 2),
            timestamp=current_time,
            metadata={**metadata, "leg": "put", "option_mid": best_put_price}
        )

        # Mark as traded to prevent duplicate signals
        self._traded_today.add((current_date, strategy_config.strategy_id))

        logger.info(
            "Emitted XSP Short Straddle signals for %s: Call strike=%s (mid=%s, qty=%d), Put strike=%s (mid=%s, qty=%d)",
            strategy_config.strategy_id,
            best_call.strike,
            round(best_call_price, 2),
            qty_call,
            best_put.strike,
            round(best_put_price, 2),
            qty_put,
        )

        return [call_signal, put_signal]
