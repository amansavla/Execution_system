"""1-minute bar construction from streaming tick data.

The backtests evaluate signals and stop-losses on 1-minute OHLC bars of
traded prices (NY-clock aligned). This module reproduces that live: every
trade print observed on a streaming ticker is folded into the current
minute's bar; completed bars are exposed for trigger evaluation.

Used for:
- Hybrid stop-loss (primary trigger = completed-bar high/low, matching
  backtest semantics; the tick-level catastrophic guard lives in
  ExitManager).
- Underlying breakout triggers evaluated on bar closes (matching the
  backtest's ``pct_from_open`` on 1-min closes).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


@dataclass
class Bar:
    """A single 1-minute OHLC bar (times in NY)."""

    symbol: str
    minute_start_ny: datetime  # NY-tz, second=0
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 0

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1


class BarBuilder:
    """Aggregates streaming tick prices into NY-aligned 1-minute bars.

    Thread/loop-safety: designed to be fed from a single asyncio loop
    (ib_async ticker callbacks). Reads (get_*) lazily roll the working bar
    into the completed deque when its minute has elapsed.
    """

    def __init__(
        self,
        max_completed_per_symbol: int = 480,
        persist_dir: Optional[str] = None,
    ) -> None:
        self._working: dict[str, Bar] = {}
        self._completed: dict[str, deque[Bar]] = {}
        self._max_completed = max_completed_per_symbol
        # When set, every completed bar is appended to
        # {persist_dir}/bars_YYYYMMDD.jsonl — the input for the Phase 6
        # shadow-replay acceptance test.
        self._persist_dir = persist_dir

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def on_tick(self, symbol: str, price: float, ts: Optional[datetime] = None) -> None:
        """Fold one observed price into the bar for its NY minute."""
        if price is None or price <= 0:
            return
        if ts is None:
            ts = datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        minute_ny = ts.astimezone(_NY).replace(second=0, microsecond=0)

        bar = self._working.get(symbol)
        if bar is None or bar.minute_start_ny != minute_ny:
            if bar is not None and bar.minute_start_ny < minute_ny:
                self._roll(symbol, bar)
            elif bar is not None and bar.minute_start_ny > minute_ny:
                # Out-of-order tick from a previous minute: ignore rather
                # than corrupt the working bar.
                return
            bar = Bar(
                symbol=symbol,
                minute_start_ny=minute_ny,
                open=price,
                high=price,
                low=price,
                close=price,
                tick_count=1,
            )
            self._working[symbol] = bar
        else:
            bar.update(price)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_latest_completed_bar(self, symbol: str, now: Optional[datetime] = None) -> Optional[Bar]:
        """Return the most recent COMPLETED 1-min bar for symbol, or None."""
        self._maybe_roll_stale_working(symbol, now)
        dq = self._completed.get(symbol)
        return dq[-1] if dq else None

    def get_completed_bars(
        self, symbol: str, count: int = 60, now: Optional[datetime] = None
    ) -> list[Bar]:
        """Return up to `count` most recent completed bars (oldest first)."""
        self._maybe_roll_stale_working(symbol, now)
        dq = self._completed.get(symbol)
        if not dq:
            return []
        return list(dq)[-count:]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _roll(self, symbol: str, bar: Bar) -> None:
        dq = self._completed.setdefault(symbol, deque(maxlen=self._max_completed))
        dq.append(bar)
        # working bar replaced by caller
        if self._persist_dir:
            self._persist(bar)

    def _persist(self, bar: Bar) -> None:
        """Append a completed bar as one JSON line (best-effort)."""
        try:
            import json
            from pathlib import Path

            d = Path(self._persist_dir)
            d.mkdir(parents=True, exist_ok=True)
            fname = d / f"bars_{bar.minute_start_ny.strftime('%Y%m%d')}.jsonl"
            with open(fname, "a") as f:
                f.write(json.dumps({
                    "symbol": bar.symbol,
                    "minute_ny": bar.minute_start_ny.isoformat(),
                    "open": bar.open, "high": bar.high,
                    "low": bar.low, "close": bar.close,
                    "ticks": bar.tick_count,
                }) + "\n")
        except Exception:  # never let persistence break bar building
            pass

    def _maybe_roll_stale_working(self, symbol: str, now: Optional[datetime]) -> None:
        """If the working bar's minute has fully elapsed, complete it.

        Without this, a symbol with no ticks in the current minute would
        never surface its last bar.
        """
        bar = self._working.get(symbol)
        if bar is None:
            return
        if now is None:
            now = datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        current_minute_ny = now.astimezone(_NY).replace(second=0, microsecond=0)
        if bar.minute_start_ny < current_minute_ny:
            self._roll(symbol, bar)
            del self._working[symbol]
