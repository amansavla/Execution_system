import logging
import re
from datetime import datetime, UTC, date, time
from zoneinfo import ZoneInfo
from typing import Optional

from src.app.runner import StrategyProvider
from src.core.config import StrategyConfig
from src.core.enums import SignalDirection, OptionRight, PositionStatus
from src.core.models import StrategySignal, OptionContract
from src.marketdata.data_quality import validate_quote_prices
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

    def __init__(
        self,
        broker: BrokerClient,
        position_manager: Optional[PositionManager] = None,
        max_spread_pct: float = 15.0,
    ) -> None:
        self.broker = broker
        self._position_manager = position_manager
        # Strikes whose quote fails this at selection time are skipped —
        # picking "closest to target premium" alone can land on a strike
        # with a flickering, thinly-quoted market (real print frozen for
        # 30s+ while bid/ask bounce), which then chases forever and never
        # fills (see 2026-06-17 stuck-exit incident). Matches risk.yaml's
        # spread_limits.max_spread_pct by default.
        self._max_spread_pct = max_spread_pct

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

        return await self._build_signals(strategy_config, current_time)

    async def emit_now(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Operator-triggered one-shot: build straddle signals immediately.

        Skips the scheduled entry-time and once-per-day gates (steps 2-4 of
        poll) — the dashboard "fire straddle" button is the caller and each
        click means exactly one straddle NOW. Runner-level guards still
        apply (system lock, entry-window exit-time bound, wait-until-flat)
        as do all risk checks (daily loss limit, 15:30 ET cutoff, spread).
        """
        logger.warning(
            "Manual straddle fire requested for %s", strategy_config.strategy_id
        )
        return await self._build_signals(strategy_config, current_time)

    async def _build_signals(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        """Scan the chain, select strikes, size and emit both legs (steps 5-12)."""
        tz_ny = ZoneInfo("America/New_York")
        time_ny = current_time.astimezone(tz_ny)
        current_date = time_ny.date()

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

        # 6b. Alternative strike-selection mode: match |delta| instead of
        # premium. Sizing (step 11) still runs off the selected strike's
        # premium regardless of mode, so this only changes *which* strike
        # gets picked.
        strike_selection = getattr(strategy_config.entry, "strike_selection", "premium_target")
        target_delta = getattr(strategy_config.entry, "target_delta", None) or 0.30

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

        # 9. Find best Call strike (closest to target premium, or closest
        # to target_delta when strike_selection == "delta_target")
        best_call: Optional[OptionContract] = None
        best_call_price: Optional[float] = None
        best_call_delta: Optional[float] = None
        best_call_diff = float("inf")

        for sym, contract in call_symbols.items():
            q = option_quotes.get(sym)
            if not q or q.bid is None or q.ask is None:
                continue
            valid, _reason = validate_quote_prices(q, self._max_spread_pct)
            if not valid:
                continue
            mid = (q.bid + q.ask) / 2.0
            if strike_selection == "delta_target":
                if q.delta is None:
                    continue
                diff = abs(q.delta - target_delta)
            else:
                diff = abs(mid - target_premium)
            if diff < best_call_diff:
                best_call_diff = diff
                best_call = contract
                best_call_price = mid
                best_call_delta = q.delta

        # 10. Find best Put strike (closest to target premium, or closest
        # to -target_delta when strike_selection == "delta_target")
        best_put: Optional[OptionContract] = None
        best_put_price: Optional[float] = None
        best_put_delta: Optional[float] = None
        best_put_diff = float("inf")

        for sym, contract in put_symbols.items():
            q = option_quotes.get(sym)
            if not q or q.bid is None or q.ask is None:
                continue
            valid, _reason = validate_quote_prices(q, self._max_spread_pct)
            if not valid:
                continue
            mid = (q.bid + q.ask) / 2.0
            if strike_selection == "delta_target":
                if q.delta is None:
                    continue
                diff = abs(q.delta + target_delta)  # put delta is negative
            else:
                diff = abs(mid - target_premium)
            if diff < best_put_diff:
                best_put_diff = diff
                best_put = contract
                best_put_price = mid
                best_put_delta = q.delta

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
            "strike_selection": strike_selection,
            "target_delta": target_delta if strike_selection == "delta_target" else None,
        }

        call_signal = StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.SHORT,
            contract=best_call,
            requested_quantity=qty_call,
            limit_price=round(best_call_price, 2),
            timestamp=current_time,
            metadata={**metadata, "leg": "call", "option_mid": best_call_price, "option_delta": best_call_delta}
        )

        put_signal = StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.SHORT,
            contract=best_put,
            requested_quantity=qty_put,
            limit_price=round(best_put_price, 2),
            timestamp=current_time,
            metadata={**metadata, "leg": "put", "option_mid": best_put_price, "option_delta": best_put_delta}
        )

        # NOTE: we deliberately do NOT mark _traded_today here. Marking on
        # *emit* burned the strategy's one daily slot even when the signal
        # was then risk-rejected (e.g. max_positions_per_underlying full on
        # a restart): the strategy never traded, yet refused to retry for
        # the rest of the day (observed 2026-06-15). The real one-trade-per-
        # day guard is the PositionManager check above (lines ~78-84), which
        # only counts ACTUAL positions; an in-flight order is covered by the
        # runner's wait-until-flat, and repeated rejections are throttled by
        # the runner's _entry_retry_after backoff. So a rejected signal no
        # longer consumes the day's slot.

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
