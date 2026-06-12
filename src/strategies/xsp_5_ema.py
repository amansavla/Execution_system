"""5 EMA intraday breakout strategy on XSP 0DTE options.

Backtest-faithful live implementation:

- 5-minute candles (NY clock, 9:30-aligned) aggregated from the broker's
  streaming 1-min BarBuilder bars.
- 5 EMA resets daily: EMA(9:35 close) = close; then
  EMA_t = close * 1/3 + EMA_{t-1} * 2/3  (alpha = 2/(5+1)).
- Signal (alert) bar: a completed 5-min bar fully ABOVE the EMA
  (low > ema) arms a PUT entry; fully BELOW (high < ema) arms a CALL.
  A newer qualifying bar rolls the levels forward; a bar touching the
  EMA cancels the armed signal.
- Entry: 1-min bar CLOSE breaking the signal bar's low (PUT) or high
  (CALL) buys the ATM option. Max trades/day per variant, one position
  at a time (runner's wait-until-flat enforces flatness).
- Exits are strategy-driven via the runner's collect_exits hook
  (ExitManager `strategy_exit` path):
    base  variant: underlying stop at signal-bar high/low, profit
                   target at entry +/- 3R on the underlying.
    trail variant: same underlying stop; option-premium trailing floor
                   (every +20% of entry premium on the PEAK locks in a
                   floor 15% higher; exit when premium falls to floor).
  Both variants exit at 15:45 NY (config time_exit_utc is the backstop).

Restart behavior: EMA history is backfilled from IBKR historical 1-min
closes at 5-min boundaries (close-only), so the EMA stays faithful after
a mid-session restart. Signal bars require live OHLC and only form from
bars built while the process is running. An open position adopted after
a restart has lost its underlying stop context — it falls back to a
premium stop 20% below entry (plus the trailing floor for the trail
variant) and logs a warning.

Config (configs/strategies.yaml):
    entry.signal_source: "xsp_5_ema"
    strategy_id xsp_5ema_base / xsp_5ema_trail select the variant via
    the code-side STRATEGY_PARAMS table below.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from src.app.runner import StrategyProvider
from src.broker.interface import BrokerClient
from src.core.config import StrategyConfig
from src.core.enums import OptionRight, OrderSide, PositionStatus, SignalDirection
from src.core.models import OptionContract, StrategySignal
from src.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

STRATEGY_PARAMS = {
    "xsp_5ema_base": {
        "trail_enabled": False,
        "rr_target": 3.0,
        "max_trades_per_day": 5,
    },
    "xsp_5ema_trail": {
        "trail_enabled": True,
        "trail_step_pct": 0.20,   # each +20% of entry premium on the peak...
        "trail_lock_pct": 0.15,   # ...locks a floor 15% (of entry) higher
        "max_trades_per_day": 5,
    },
}

# No NEW entries at/after this NY time (positions still run to 15:45).
ENTRY_CUTOFF_NY = time(15, 30)
# Hard strategy-side time exit (config time_exit_utc should match).
TIME_EXIT_NY = time(15, 45)
# Premium stop for positions adopted without underlying-stop context
# (process restart while a 5EMA position was open).
FALLBACK_PREMIUM_STOP_PCT = 0.20
# Historical 5-min-close fetches allowed per update (keeps ticks fast;
# the EMA catches up over a few ticks after a late start).
MAX_BACKFILL_FETCHES_PER_UPDATE = 3

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


class XSP5EMAStrategyProvider(StrategyProvider):
    """5 EMA breakout on XSP 0DTE — long ATM CALL/PUT with strategy exits."""

    STRATEGY_PARAMS = STRATEGY_PARAMS

    def __init__(self, broker: BrokerClient, position_manager: Optional[PositionManager] = None) -> None:
        self.broker = broker
        self._position_manager = position_manager

        # --- shared market state (one underlying => shared across variants)
        self._day: Optional[date] = None
        self._ema: Optional[float] = None
        # Next 5-min boundary close time (NY) still to process, e.g. 9:35.
        self._next_5min_close: Optional[datetime] = None
        # {'direction': 'CALL'|'PUT', 'high': float, 'low': float,
        #  'bar_time': datetime (5-min close time NY)}
        self._signal_bar: Optional[dict] = None
        self._backfill_failed_day: Optional[date] = None

        # --- per-strategy_id state
        self._trades_today: dict[str, int] = {}
        # signal-bar close time already used for an entry by this strategy
        self._consumed_bar_time: dict[str, datetime] = {}

        # --- per-position exit context
        # position_id -> {'direction', 'stop_underlying', 'entry_underlying',
        #                 'entry_price', 'peak_price', 'locked_floor'}
        self._pos_ctx: dict[UUID, dict] = {}
        # strategy_id -> ctx emitted with the signal, bound to the position
        # once the fill creates it.
        self._pending_ctx: dict[str, dict] = {}

    def set_position_manager(self, position_manager: PositionManager) -> None:
        self._position_manager = position_manager

    # ------------------------------------------------------------------
    # Market state: 5-min bars + daily-reset EMA + signal bar
    # ------------------------------------------------------------------

    def _reset_day(self, d: date) -> None:
        self._day = d
        self._ema = None
        self._signal_bar = None
        self._next_5min_close = datetime.combine(d, time(9, 35), tzinfo=_NY)
        self._trades_today = {}
        self._consumed_bar_time = {}
        logger.info("5EMA: state reset for %s", d)

    def _build_5min_bar_live(self, window_start: datetime, window_end: datetime) -> Optional[dict]:
        """Aggregate BarBuilder 1-min bars into one 5-min OHLC bar.

        Returns None unless the builder was live for the WHOLE window
        (otherwise the high/low would be incomplete and could arm a
        signal bar off partial data).
        """
        try:
            bars = self.broker.get_completed_bars("XSP", count=480)
        except Exception:
            return None
        if not bars:
            return None
        # Builder must have coverage from before the window opened.
        if bars[0].minute_start_ny > window_start:
            return None
        in_window = [b for b in bars if window_start <= b.minute_start_ny < window_end]
        if not in_window:
            return None
        return {
            "open": in_window[0].open,
            "high": max(b.high for b in in_window),
            "low": min(b.low for b in in_window),
            "close": in_window[-1].close,
        }

    async def _update_market_state(self, current_time: datetime) -> None:
        """Process every completed 5-min bar up to now: EMA + signal bar."""
        now_ny = current_time.astimezone(_NY)
        if self._day != now_ny.date():
            self._reset_day(now_ny.date())

        fetches = 0
        while self._next_5min_close is not None and now_ny >= self._next_5min_close:
            close_t = self._next_5min_close
            if close_t.time() > RTH_CLOSE:
                break  # session done; nothing further today
            window_start = close_t - timedelta(minutes=5)

            bar = self._build_5min_bar_live(window_start, close_t)
            close_px: Optional[float] = None
            if bar is not None:
                close_px = bar["close"]
            else:
                # Restart/late-start path: historical close-only backfill.
                if self._backfill_failed_day == self._day:
                    self._next_5min_close = close_t + timedelta(minutes=5)
                    continue
                if fetches >= MAX_BACKFILL_FETCHES_PER_UPDATE:
                    return  # budget spent; resume next tick
                fetches += 1
                try:
                    close_px = await self.broker.get_historical_close("XSP", close_t)
                except Exception as e:
                    logger.error("5EMA: historical backfill failed at %s: %s", close_t, e)
                    close_px = None
                if close_px is None:
                    # Don't hammer IBKR all day if history is unavailable.
                    self._backfill_failed_day = self._day
                    logger.warning(
                        "5EMA: no historical close for %s — EMA will seed "
                        "from live bars only (signal quality degraded until "
                        "enough bars accumulate)", close_t,
                    )
                    self._next_5min_close = close_t + timedelta(minutes=5)
                    continue

            # EMA update (daily reset: first bar of the day seeds the EMA)
            if self._ema is None:
                self._ema = close_px
            else:
                self._ema = close_px * (1.0 / 3.0) + self._ema * (2.0 / 3.0)

            # Signal-bar rules need full OHLC -> live bars only.
            if bar is not None and self._ema is not None:
                if bar["low"] > self._ema:
                    self._signal_bar = {
                        "direction": "PUT", "high": bar["high"], "low": bar["low"],
                        "bar_time": close_t,
                    }
                    logger.info(
                        "5EMA: PUT alert bar @ %s [%.2f-%.2f] ema=%.2f",
                        close_t.strftime("%H:%M"), bar["low"], bar["high"], self._ema,
                    )
                elif bar["high"] < self._ema:
                    self._signal_bar = {
                        "direction": "CALL", "high": bar["high"], "low": bar["low"],
                        "bar_time": close_t,
                    }
                    logger.info(
                        "5EMA: CALL alert bar @ %s [%.2f-%.2f] ema=%.2f",
                        close_t.strftime("%H:%M"), bar["low"], bar["high"], self._ema,
                    )
                elif self._signal_bar is not None:
                    logger.info(
                        "5EMA: signal bar cancelled — bar @ %s touched ema=%.2f",
                        close_t.strftime("%H:%M"), self._ema,
                    )
                    self._signal_bar = None

            self._next_5min_close = close_t + timedelta(minutes=5)

    # ------------------------------------------------------------------
    # Position context binding (signal -> fill -> position)
    # ------------------------------------------------------------------

    def _sync_position_contexts(self, strategy_id: str, params: dict) -> list:
        """Bind pending entry context to new positions; drop closed ones.

        Returns this strategy's open positions.
        """
        if self._position_manager is None:
            return []
        open_positions = [
            p for p in self._position_manager.positions.values()
            if p.strategy_id == strategy_id
            and p.status in (PositionStatus.OPENING, PositionStatus.OPEN)
        ]
        open_ids = {p.position_id for p in open_positions}
        stale = [
            pid for pid, ctx in self._pos_ctx.items()
            if ctx.get("strategy_id") == strategy_id and pid not in open_ids
        ]
        for pid in stale:
            self._pos_ctx.pop(pid)
            logger.info("5EMA: position %s closed, context dropped", pid)

        for pos in open_positions:
            if pos.position_id in self._pos_ctx:
                continue
            pending = self._pending_ctx.pop(strategy_id, None)
            if pending is not None:
                pending["entry_price"] = pos.average_entry_price
                pending["peak_price"] = pos.average_entry_price
                pending["strategy_id"] = strategy_id
                self._pos_ctx[pos.position_id] = pending
                logger.info(
                    "5EMA: bound context to position %s (dir=%s stop_underlying=%.2f entry_premium=%.2f)",
                    pos.position_id, pending["direction"],
                    pending["stop_underlying"], pos.average_entry_price,
                )
            else:
                # Restart adoption: underlying stop context is gone.
                right = pos.contract.right.value if hasattr(pos.contract.right, "value") else str(pos.contract.right)
                self._pos_ctx[pos.position_id] = {
                    "strategy_id": strategy_id,
                    "direction": "CALL" if right.upper().startswith("C") else "PUT",
                    "stop_underlying": None,
                    "entry_underlying": None,
                    "entry_price": pos.average_entry_price,
                    "peak_price": pos.average_entry_price,
                    "locked_floor": None,
                }
                logger.warning(
                    "5EMA: adopted position %s with no entry context (restart?) — "
                    "falling back to premium stop %.0f%% below entry",
                    pos.position_id, FALLBACK_PREMIUM_STOP_PCT * 100,
                )
        return open_positions

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _latest_underlying_close(self) -> Optional[float]:
        """Latest completed 1-min bar close (backtest-faithful trigger price)."""
        try:
            bar = self.broker.get_latest_completed_bar("XSP")
        except Exception:
            bar = None
        return bar.close if bar is not None else None

    async def _underlying_price(self) -> Optional[float]:
        """1-min bar close, falling back to live quote when no bars yet."""
        px = self._latest_underlying_close()
        if px is not None:
            return px
        try:
            q = (await self.broker.get_quotes(["XSP"])).get("XSP")
        except Exception:
            return None
        if q is None:
            return None
        if q.bid is not None and q.ask is not None:
            return (q.bid + q.ask) / 2.0
        return q.last or q.close

    async def _option_mid(self, contract: OptionContract) -> Optional[float]:
        key = contract.to_quote_symbol()
        try:
            q = (await self.broker.get_quotes([key])).get(key)
        except Exception:
            return None
        if q is None:
            return None
        if q.bid is not None and q.ask is not None:
            return (q.bid + q.ask) / 2.0
        return q.last

    # ------------------------------------------------------------------
    # Entry: poll
    # ------------------------------------------------------------------

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        params = self.STRATEGY_PARAMS.get(strategy_config.strategy_id)
        if params is None:
            logger.warning("5EMA: unknown strategy_id %s", strategy_config.strategy_id)
            return []

        await self._update_market_state(current_time)
        self._sync_position_contexts(strategy_config.strategy_id, params)

        now_ny = current_time.astimezone(_NY)
        if now_ny.weekday() >= 5:
            return []
        if not (RTH_OPEN <= now_ny.time() < ENTRY_CUTOFF_NY):
            return []
        if self._trades_today.get(strategy_config.strategy_id, 0) >= params["max_trades_per_day"]:
            return []
        # Runner enforces wait-until-flat; this is a belt-and-braces check.
        if self._position_manager is not None and any(
            p.strategy_id == strategy_config.strategy_id
            and p.status in (PositionStatus.OPENING, PositionStatus.OPEN)
            for p in self._position_manager.positions.values()
        ):
            return []

        sig = self._signal_bar
        if sig is None:
            return []
        if self._consumed_bar_time.get(strategy_config.strategy_id) == sig["bar_time"]:
            return []  # already traded off this signal bar

        underlying_close = self._latest_underlying_close()
        if underlying_close is None:
            return []

        # Breakout on 1-min close beyond the signal bar's level
        if sig["direction"] == "PUT" and underlying_close <= sig["low"]:
            right = OptionRight.PUT
            stop_underlying = sig["high"]
        elif sig["direction"] == "CALL" and underlying_close >= sig["high"]:
            right = OptionRight.CALL
            stop_underlying = sig["low"]
        else:
            return []

        contract = OptionContract(
            symbol="XSP",
            expiry=now_ny.date().strftime("%Y%m%d"),
            strike=float(round(underlying_close)),
            right=right,
            multiplier=100,
        )

        # Dynamic sizing: equity * position_sizing_pct / option premium,
        # capped by entry.max_contracts (same pattern as xsp_breakout).
        option_mid = await self._option_mid(contract)
        position_sizing_pct = getattr(strategy_config, "position_sizing_pct", None) or 0.01
        available_cap: Optional[float] = None
        try:
            account_state = await self.broker.get_account_state()
            available_cap = account_state.net_liquidation
        except Exception as e:
            logger.warning("5EMA: account state unavailable for sizing: %s", e)
        if option_mid and option_mid > 0 and available_cap and available_cap > 0:
            qty = max(1, int(available_cap * position_sizing_pct / (option_mid * 100.0)))
            qty = min(qty, strategy_config.entry.max_contracts)
        else:
            logger.warning(
                "5EMA: sizing inputs unavailable (mid=%s netliq=%s); qty=1",
                option_mid, available_cap,
            )
            qty = 1

        self._consumed_bar_time[strategy_config.strategy_id] = sig["bar_time"]
        self._trades_today[strategy_config.strategy_id] = (
            self._trades_today.get(strategy_config.strategy_id, 0) + 1
        )
        self._pending_ctx[strategy_config.strategy_id] = {
            "direction": right.value if hasattr(right, "value") else str(right),
            "stop_underlying": stop_underlying,
            "entry_underlying": underlying_close,
            "entry_price": option_mid,  # refined to avg fill on binding
            "peak_price": option_mid,
            "locked_floor": None,
        }

        logger.warning(
            "5EMA ENTRY %s: %s %s strike=%s (signal bar %s [%.2f-%.2f], "
            "underlying=%.2f, stop=%.2f, trade %d/%d)",
            strategy_config.strategy_id, sig["direction"], contract.expiry,
            contract.strike, sig["bar_time"].strftime("%H:%M"),
            sig["low"], sig["high"], underlying_close, stop_underlying,
            self._trades_today[strategy_config.strategy_id],
            params["max_trades_per_day"],
        )

        return [StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=SignalDirection.LONG,
            contract=contract,
            requested_quantity=qty,
            limit_price=round(option_mid, 2) if option_mid else None,
            timestamp=current_time,
            metadata={
                "trigger_type": f"5ema_breakout_{sig['direction'].lower()}",
                "signal_bar_high": sig["high"],
                "signal_bar_low": sig["low"],
                "signal_bar_time": sig["bar_time"].isoformat(),
                "ema": self._ema,
                "underlying_at_entry": underlying_close,
                "stop_underlying": stop_underlying,
                "trail_enabled": params["trail_enabled"],
            },
        )]

    # ------------------------------------------------------------------
    # Exits: collect_exits (runner hook -> ExitManager strategy_exit path)
    # ------------------------------------------------------------------

    async def collect_exits(self, strategy_config: StrategyConfig, current_time: datetime) -> set[UUID]:
        params = self.STRATEGY_PARAMS.get(strategy_config.strategy_id)
        if params is None:
            return set()

        await self._update_market_state(current_time)
        open_positions = self._sync_position_contexts(strategy_config.strategy_id, params)
        if not open_positions:
            return set()

        exits: set[UUID] = set()
        now_ny = current_time.astimezone(_NY)
        underlying = await self._underlying_price()

        for pos in open_positions:
            ctx = self._pos_ctx.get(pos.position_id)
            if ctx is None:
                continue
            reason: Optional[str] = None

            # 1. Time exit (ExitManager time_exit_utc is the backstop)
            if now_ny.time() >= TIME_EXIT_NY:
                reason = "time_15:45"

            # 2. Underlying stop (signal bar high/low) — both variants
            if reason is None and underlying is not None and ctx["stop_underlying"] is not None:
                if ctx["direction"] == "CALL" and underlying <= ctx["stop_underlying"]:
                    reason = f"underlying_stop({underlying:.2f}<={ctx['stop_underlying']:.2f})"
                elif ctx["direction"] == "PUT" and underlying >= ctx["stop_underlying"]:
                    reason = f"underlying_stop({underlying:.2f}>={ctx['stop_underlying']:.2f})"

            if not params["trail_enabled"]:
                # 3a. Base case: 3R profit target on the underlying
                if (
                    reason is None and underlying is not None
                    and ctx["stop_underlying"] is not None
                    and ctx["entry_underlying"] is not None
                ):
                    risk = abs(ctx["entry_underlying"] - ctx["stop_underlying"])
                    rr = params["rr_target"]
                    if risk > 0:
                        if ctx["direction"] == "CALL" and underlying >= ctx["entry_underlying"] + rr * risk:
                            reason = f"target_3R({underlying:.2f})"
                        elif ctx["direction"] == "PUT" and underlying <= ctx["entry_underlying"] - rr * risk:
                            reason = f"target_3R({underlying:.2f})"
            else:
                # 3b. Trail case: option-premium trailing floor.
                mid = await self._option_mid(pos.contract)
                entry = ctx.get("entry_price") or pos.average_entry_price
                if mid is not None and entry and entry > 0:
                    # Floor from the PREVIOUS peak first (no lookahead),
                    # then roll the peak forward with the current price.
                    step = params["trail_step_pct"]
                    lock = params["trail_lock_pct"]
                    steps = int((ctx["peak_price"] - entry) / (entry * step)) if ctx["peak_price"] else 0
                    if steps > 0:
                        ctx["locked_floor"] = entry + steps * (entry * lock)
                    if reason is None and ctx.get("locked_floor") is not None and mid <= ctx["locked_floor"]:
                        reason = f"trail_floor({mid:.2f}<={ctx['locked_floor']:.2f})"
                    if mid > (ctx["peak_price"] or 0):
                        ctx["peak_price"] = mid

            # 4. Fallback premium stop for adopted positions (no underlying
            #    stop context after a restart).
            if reason is None and ctx["stop_underlying"] is None:
                mid = await self._option_mid(pos.contract)
                entry = ctx.get("entry_price") or pos.average_entry_price
                if mid is not None and entry and mid <= entry * (1.0 - FALLBACK_PREMIUM_STOP_PCT):
                    reason = f"fallback_premium_stop({mid:.2f})"

            if reason is not None:
                logger.warning(
                    "5EMA EXIT %s position %s: %s",
                    strategy_config.strategy_id, pos.position_id, reason,
                )
                exits.add(pos.position_id)

        return exits
