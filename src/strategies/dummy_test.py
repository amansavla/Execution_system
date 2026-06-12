"""TEMP test strategy: emits one small dummy order to exercise the full
execution path live — entry fill, slippage instrumentation, stop-loss /
time-exit, dashboard visibility. Remove (or disable in config) after the
live shake-out.

Direction comes from config (`direction: long|short`); strike is ATM.
One contract, once per day per strategy_id.
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from src.app.runner import StrategyProvider
from src.broker.interface import BrokerClient
from src.core.config import StrategyConfig
from src.core.enums import OptionRight, SignalDirection
from src.core.models import OptionContract, StrategySignal
from src.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)


class DummyTestStrategyProvider(StrategyProvider):
    """One-shot ATM order for execution-path shakeout."""

    def __init__(self, broker: BrokerClient, position_manager: Optional[PositionManager] = None) -> None:
        self.broker = broker
        self._position_manager = position_manager
        self._traded_today: set[tuple[date, str]] = set()

    def set_position_manager(self, pm: PositionManager) -> None:
        self._position_manager = pm

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        tz_ny = ZoneInfo("America/New_York")
        today = current_time.astimezone(tz_ny).date()
        key = (today, strategy_config.strategy_id)
        if key in self._traded_today:
            return []

        # Restart dedup via PositionManager
        if self._position_manager:
            for pos in self._position_manager.positions.values():
                if pos.strategy_id == strategy_config.strategy_id:
                    pos_t = (pos.entry_time or pos.created_at).astimezone(tz_ny)
                    if pos_t.date() == today:
                        return []

        # Underlying quote -> ATM strike
        try:
            quotes = await self.broker.get_quotes(["XSP"])
            q = quotes.get("XSP")
        except Exception as e:
            logger.error("dummy_test: quote fetch failed: %s", e)
            return []
        if not q:
            return []
        px = None
        if q.bid is not None and q.ask is not None:
            px = (q.bid + q.ask) / 2.0
        elif q.last is not None:
            px = q.last
        elif q.close is not None:
            px = q.close
        if not px:
            return []

        is_long = (getattr(strategy_config, "direction", "long") or "long") == "long"
        # Long test: buy ATM call. Short test: sell a further OTM put
        # (cheap premium, defined small risk for a shakeout).
        if is_long:
            right = OptionRight.CALL
            strike = float(round(px))
            direction = SignalDirection.LONG
        else:
            right = OptionRight.PUT
            strike = float(round(px) - 3)
            direction = SignalDirection.SHORT

        contract = OptionContract(
            symbol="XSP",
            expiry=today.strftime("%Y%m%d"),
            strike=strike,
            right=right,
            multiplier=100,
        )

        # Option mid for the limit
        try:
            oq = (await self.broker.get_quotes([contract.to_quote_symbol()])).get(contract.to_quote_symbol())
        except Exception:
            oq = None
        limit = None
        if oq and oq.bid is not None and oq.ask is not None:
            limit = round((oq.bid + oq.ask) / 2.0, 2)

        self._traded_today.add(key)
        logger.warning(
            "DUMMY TEST signal: %s %s ATM strike=%s limit=%s (strategy=%s)",
            direction.value, right.value, strike, limit, strategy_config.strategy_id,
        )
        return [StrategySignal(
            strategy_id=strategy_config.strategy_id,
            direction=direction,
            contract=contract,
            requested_quantity=1,
            limit_price=limit,
            timestamp=current_time,
            metadata={"purpose": "execution_shakeout", "underlying_px": px},
        )]
