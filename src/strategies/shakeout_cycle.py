"""TEMP shakeout strategy: cycles entry -> hold -> exit repeatedly.

Purpose: real-time execution testing (incl. GTH overnight sessions when
the market is closed). Each cycle buys a cheap OTM call AND a cheap OTM
put (defined-risk, two legs => exercises multi-leg coordination and
replacement-chain repricing), holds for exit.max_hold_seconds, exits via
the normal ExitManager path, waits cooldown_seconds, repeats.

Config (signal_source: shakeout_cycle):
    entry.max_contracts: per-leg qty (keep 1)
    exit.max_hold_seconds: hold time per cycle (e.g. 180)
    cooldown_seconds: wait between cycles (e.g. 120)
    metadata via strategy_overrides as usual

Picks the nearest tradable expiry: today before 16:00 NY, else next
weekday (GTH trades next-day contracts overnight). Strikes ~3 points OTM
for cheap premium. REMOVE/disable after testing.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from src.app.runner import StrategyProvider
from src.broker.interface import BrokerClient
from src.core.config import StrategyConfig
from src.core.enums import OptionRight, SignalDirection
from src.core.models import OptionContract, StrategySignal
from src.portfolio.position_manager import PositionManager

logger = logging.getLogger(__name__)


class ShakeoutCycleStrategyProvider(StrategyProvider):
    """Cycling long OTM call+put for live execution shakeout."""

    def __init__(self, broker: BrokerClient, position_manager: Optional[PositionManager] = None) -> None:
        self.broker = broker
        self._position_manager = position_manager
        self._last_cycle_end: Optional[datetime] = None
        self._cycles = 0

    def set_position_manager(self, pm: PositionManager) -> None:
        self._position_manager = pm

    def _target_expiry(self, now_ny: datetime) -> str:
        d = now_ny.date()
        if now_ny.hour >= 16:  # after the close -> next session's contracts
            d += timedelta(days=1)
        while d.weekday() >= 5:  # skip weekend
            d += timedelta(days=1)
        return d.strftime("%Y%m%d")

    async def poll(self, strategy_config: StrategyConfig, current_time: datetime) -> list[StrategySignal]:
        # Gate: only one cycle live at a time; the runner's wait-until-flat
        # guard handles open positions/orders, we add the cooldown.
        if self._position_manager:
            for pos in self._position_manager.positions.values():
                if pos.strategy_id == strategy_config.strategy_id and pos.status.value in ("open", "opening", "OPEN", "OPENING"):
                    self._last_cycle_end = None  # will reset on flat
                    return []
        cooldown = strategy_config.cooldown_seconds or 120
        if self._last_cycle_end is None:
            # First poll after being flat: start cooldown clock
            self._last_cycle_end = current_time
            return []
        if (current_time - self._last_cycle_end).total_seconds() < cooldown:
            return []

        # Underlying
        try:
            q = (await self.broker.get_quotes(["XSP"])).get("XSP")
        except Exception as e:
            logger.error("shakeout: quote fetch failed: %s", e)
            return []
        px = None
        if q:
            if q.bid is not None and q.ask is not None:
                px = (q.bid + q.ask) / 2.0
            elif q.last is not None:
                px = q.last
            elif q.close is not None:
                px = q.close
        if not px:
            return []

        now_ny = current_time.astimezone(ZoneInfo("America/New_York"))
        expiry = self._target_expiry(now_ny)
        call_strike = float(round(px) + 3)
        put_strike = float(round(px) - 3)
        qty = max(1, min(strategy_config.entry.max_contracts, 2))

        signals = []
        for strike, right in ((call_strike, OptionRight.CALL), (put_strike, OptionRight.PUT)):
            contract = OptionContract(symbol="XSP", expiry=expiry, strike=strike,
                                      right=right, multiplier=100)
            limit = None
            try:
                oq = (await self.broker.get_quotes([contract.to_quote_symbol()])).get(contract.to_quote_symbol())
                if oq and oq.bid is not None and oq.ask is not None:
                    limit = round((oq.bid + oq.ask) / 2.0, 2)
            except Exception:
                pass
            signals.append(StrategySignal(
                strategy_id=strategy_config.strategy_id,
                direction=SignalDirection.LONG,
                contract=contract,
                requested_quantity=qty,
                limit_price=limit,
                timestamp=current_time,
                metadata={"purpose": "shakeout_cycle", "cycle": self._cycles + 1,
                          "underlying_px": px},
            ))

        self._cycles += 1
        self._last_cycle_end = None
        logger.warning(
            "SHAKEOUT cycle %d: long %sC + %sP exp %s (underlying %.2f)",
            self._cycles, call_strike, put_strike, expiry, px,
        )
        return signals
