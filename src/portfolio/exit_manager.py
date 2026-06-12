"""ExitManager for evaluating open positions and emitting exit OrderIntents.

Evaluates stop loss, take profit, time exit, strategy-driven exits, and
force flattening conditions.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from src.core.enums import OrderSide, PositionStatus
from src.core.models import OrderIntent, Position, QuoteSnapshot
from src.storage.event_log import EventStore
from src.marketdata.data_quality import validate_quote_freshness, validate_quote_prices

logger = logging.getLogger(__name__)


class ExitManager:
    """Evaluates open position exit conditions and generates exit OrderIntents.

    Strictly isolated from BrokerClient and OrderManager.
    """

    def __init__(self, event_store: EventStore) -> None:
        """Initialize ExitManager.

        Args:
            event_store: EventStore for persisting audit logs.
        """
        self.event_store = event_store

    # Catastrophic intrabar guard: tick-mid must breach the stop by this
    # extra fraction before firing without waiting for the bar to complete.
    INTRABAR_GUARD_BUFFER = 0.15

    def check_exits(
        self,
        positions: list[Position],
        quotes: dict[str, QuoteSnapshot],
        current_time: datetime,
        force_flatten_all: bool = False,
        strategy_exits: Optional[set[UUID]] = None,
        bars: Optional[dict[str, object]] = None,
        max_spread_pct: Optional[float] = None,
        max_age_seconds: Optional[float] = None,
    ) -> list[tuple[Position, OrderIntent, str]]:
        """Evaluate open positions and return list of (Position, exit_intent, trigger_reason).

        Args:
            positions: List of all positions to evaluate.
            quotes: Map of symbol to QuoteSnapshot.
            current_time: Current time in UTC.
            force_flatten_all: If True, forces flat exits on all open positions.
            strategy_exits: Optional set of position_ids flagged by strategies for exit.
            bars: Optional map of symbol -> latest completed 1-min Bar. When
                provided, stop-losses use HYBRID evaluation: primary trigger
                on completed-bar high/low (matching backtest semantics) plus
                a tick-mid catastrophic guard at stop*(1 +/- buffer). When
                absent, legacy tick bid/ask evaluation applies.
            max_spread_pct: Optional maximum allowed spread percentage for quote.
            max_age_seconds: Optional maximum allowed age of quote in seconds.
        """
        exit_signals: list[tuple[Position, OrderIntent, str]] = []
        strategy_exits = strategy_exits or set()
        bars = bars or {}

        for pos in positions:
            # We only evaluate active open positions
            if pos.status not in (PositionStatus.OPENING, PositionStatus.OPEN):
                continue

            trigger_reason: Optional[str] = None
            limit_price: Optional[float] = None

            # Retrieve QuoteSnapshot for evaluation (prioritize option quote key)
            opt_key = pos.contract.to_quote_symbol() if hasattr(pos.contract, "to_quote_symbol") else None
            quote = None
            if opt_key and opt_key in quotes:
                quote = quotes[opt_key]
            else:
                quote = quotes.get(pos.contract.symbol)

            # 1. Force Flatten Check (highest priority)
            if force_flatten_all:
                trigger_reason = "force_flatten"
                # Use best quote for flattening
                if quote:
                    limit_price = quote.bid if pos.side == OrderSide.BUY else quote.ask
                if limit_price is None and pos.current_price is not None:
                    limit_price = pos.current_price
            
            # 2. Time Exit Check
            elif pos.time_exit_utc is not None and current_time >= pos.time_exit_utc:
                trigger_reason = "time_exit"
                if quote:
                    limit_price = quote.bid if pos.side == OrderSide.BUY else quote.ask
                if limit_price is None and pos.current_price is not None:
                    limit_price = pos.current_price

            # 3. Strategy-Generated Exit Check
            elif pos.position_id in strategy_exits:
                trigger_reason = "strategy_exit"
                if quote:
                    limit_price = quote.bid if pos.side == OrderSide.BUY else quote.ask
                if limit_price is None and pos.current_price is not None:
                    limit_price = pos.current_price

            # 4. Stop Loss / Take Profit Check (requires quote)
            elif quote is not None:
                # Validate quote freshness if age limit is set
                if max_age_seconds is not None:
                    is_fresh, reason = validate_quote_freshness(quote, current_time, max_age_seconds)
                    if not is_fresh:
                        logger.warning(
                            "Exit check: skipping stop/target evaluation for position %s due to stale quote: %s",
                            pos.position_id, reason
                        )
                        continue

                # Validate quote spread if limit is set
                if max_spread_pct is not None:
                    is_valid_spread, reason = validate_quote_prices(quote, max_spread_pct)
                    if not is_valid_spread:
                        logger.warning(
                            "Exit check: skipping stop/target evaluation for position %s due to invalid spread: %s",
                            pos.position_id, reason
                        )
                        continue

                # Grace period: skip stop-loss checks in the first 10 seconds after entry
                # to avoid false triggers caused by bid-ask spread at entry time
                entry_time = pos.entry_time or pos.created_at
                seconds_since_entry = (current_time - entry_time).total_seconds()
                if seconds_since_entry < 10.0:
                    logger.debug(
                        "Position %s: skipping stop/target check — only %.1fs since entry (grace=10s)",
                        pos.position_id, seconds_since_entry,
                    )
                    continue

                # Find evaluation price (mid or bid for selling long, mid or ask for buying back short)
                eval_price = None
                if getattr(pos, "use_mid_for_exits", False):
                    if quote.bid is not None and quote.ask is not None:
                        eval_price = (quote.bid + quote.ask) / 2.0
                    elif pos.side == OrderSide.BUY:
                        eval_price = quote.bid
                    else:
                        eval_price = quote.ask
                else:
                    if pos.side == OrderSide.BUY:
                        eval_price = quote.bid
                    else:
                        eval_price = quote.ask

                # Hybrid stop-loss evaluation when a completed 1-min bar is
                # available for this contract (matches backtest semantics):
                #   primary: bar.low (long) / bar.high (short) vs stop level
                #   guard:   tick MID breaching stop*(1 -/+ buffer) fires
                #            immediately intrabar (gap protection)
                # Legacy tick bid/ask evaluation applies when no bar exists.
                bar = bars.get(opt_key) if opt_key else None
                if bar is None:
                    bar = bars.get(pos.contract.symbol)

                # Only bars strictly AFTER the entry minute count. The completed bar
                # for the entry minute is discarded because it contains price action/ticks
                # that occurred BEFORE the position was entered, causing false stop triggers.
                # Live ticks will still be evaluated for stop-losses during the entry minute.
                if bar is not None:
                    entry_minute = entry_time.astimezone(
                        bar.minute_start_ny.tzinfo
                    ).replace(second=0, microsecond=0)
                    if bar.minute_start_ny <= entry_minute:
                        bar = None

                mid_price = None
                if quote.bid is not None and quote.ask is not None:
                    mid_price = (quote.bid + quote.ask) / 2.0

                stop_hit = False
                if pos.stop_price is not None:
                    if bar is not None:
                        # Primary (bar-based, backtest-faithful)
                        if pos.side == OrderSide.BUY and bar.low <= pos.stop_price:
                            stop_hit = True
                        elif pos.side == OrderSide.SELL and bar.high >= pos.stop_price:
                            stop_hit = True
                        # Catastrophic intrabar guard (tick mid, buffered)
                        if not stop_hit and mid_price is not None:
                            buf = self.INTRABAR_GUARD_BUFFER
                            if pos.side == OrderSide.BUY and mid_price <= pos.stop_price * (1 - buf):
                                stop_hit = True
                                logger.warning(
                                    "Intrabar guard fired for %s: mid=%.2f << stop=%.2f",
                                    pos.position_id, mid_price, pos.stop_price,
                                )
                            elif pos.side == OrderSide.SELL and mid_price >= pos.stop_price * (1 + buf):
                                stop_hit = True
                                logger.warning(
                                    "Intrabar guard fired for %s: mid=%.2f >> stop=%.2f",
                                    pos.position_id, mid_price, pos.stop_price,
                                )
                    elif eval_price is not None:
                        # Legacy tick-quote evaluation (no bar data yet)
                        if pos.side == OrderSide.BUY and eval_price <= pos.stop_price:
                            stop_hit = True
                        elif pos.side == OrderSide.SELL and eval_price >= pos.stop_price:
                            stop_hit = True

                if stop_hit:
                    trigger_reason = "stop_loss"
                    # Exit at the touch (marketable, inside NBBO)
                    if pos.side == OrderSide.BUY:
                        limit_price = quote.bid if quote.bid is not None else eval_price
                    else:
                        limit_price = quote.ask if quote.ask is not None else eval_price
                elif eval_price is not None and pos.target_price is not None:
                    # Take-profit stays tick-quote based
                    if pos.side == OrderSide.BUY and eval_price >= pos.target_price:
                        trigger_reason = "take_profit"
                        limit_price = eval_price
                    elif pos.side == OrderSide.SELL and eval_price <= pos.target_price:
                        trigger_reason = "take_profit"
                        limit_price = eval_price

                if pos.stop_price is not None or pos.target_price is not None:
                    logger.debug(
                        "Position %s (%s side=%s): bar=%s, eval=%s, mid=%s, stop=%s, target=%s, trigger=%s",
                        pos.position_id,
                        opt_key or pos.contract.symbol,
                        pos.side.value,
                        f"[{bar.low:.2f}-{bar.high:.2f}]" if bar else None,
                        f"{eval_price:.2f}" if eval_price is not None else None,
                        f"{mid_price:.2f}" if mid_price is not None else None,
                        pos.stop_price,
                        pos.target_price,
                        trigger_reason,
                    )

            # If an exit was triggered, create and append OrderIntent
            if trigger_reason is not None:
                # NO entry-price fallback: an exit limit must come from live
                # market data (quote touch) or the position's current price.
                # Pricing an exit off average_entry_price produced limits far
                # outside NBBO (IBKR Error 202 cancel loops). If we have no
                # price at all, skip this tick — the exit re-triggers next
                # tick once a quote arrives. Exception: force_flatten must
                # always go out, using current_price as last resort.
                if limit_price is None:
                    if trigger_reason == "force_flatten" and pos.current_price is not None:
                        limit_price = pos.current_price
                    else:
                        logger.error(
                            "Exit %s triggered for position %s but no usable quote "
                            "(bid=%s, ask=%s, current_price=%s); deferring to next tick.",
                            trigger_reason,
                            pos.position_id,
                            quote.bid if quote else None,
                            quote.ask if quote else None,
                            pos.current_price,
                        )
                        self.event_store.log_callback("exit_deferred_no_quote", {
                            "position_id": pos.position_id,
                            "trigger": trigger_reason,
                            "timestamp": current_time.isoformat(),
                        })
                        continue

                # Exit limits are placed AT the touch (sell -> bid, buy -> ask)
                # — marketable but inside NBBO. The repricer chases from there.
                # No beyond-touch offset: that pushed limits outside NBBO.

                # Ensure limit_price is valid positive number
                if limit_price <= 0:
                    limit_price = 0.01

                limit_price = round(limit_price, 2)

                # Generate the exit OrderIntent (reduce-only: is_entry = False)
                exit_intent = OrderIntent(
                    signal_id=uuid4(),
                    risk_decision_id=uuid4(),  # matched by runner with RiskEngine approval
                    position_id=pos.position_id,
                    is_entry=False,
                    strategy_id=pos.strategy_id,
                    contract=pos.contract,
                    side=OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY,
                    quantity=pos.quantity,
                    limit_price=limit_price,
                    timestamp=current_time,
                )

                exit_signals.append((pos, exit_intent, trigger_reason))

                self.event_store.log_callback("exit_triggered", {
                    "position_id": pos.position_id,
                    "trigger": trigger_reason,
                    "contract": f"{pos.contract.symbol} {pos.contract.expiry} {pos.contract.strike} {pos.contract.right}",
                    "quantity": pos.quantity,
                    "exit_limit_price": limit_price,
                    "timestamp": current_time.isoformat(),
                })

        return exit_signals
