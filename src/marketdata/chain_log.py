"""Persist option-chain quote snapshots for shadow-replay of premium-driven
strategy decisions.

The underlying 1-min bars (data/bars/) let us replay *underlying-driven*
logic (breakout triggers, 5EMA signal/stop/target). But two things are NOT
reproducible from underlying bars:

  - the short-straddle's STRIKE SELECTION — it scans the option chain for the
    strike whose premium is closest to a target, and
  - the 5EMA trail variant's EXIT — it tracks the held option's premium peak.

Both depend on option premiums that bars don't contain. This writer snapshots
every option quote the broker serves (throttled per symbol) to
``data/chains/chain_YYYYMMDD.jsonl`` so those decisions can be replayed
faithfully after the fact and diffed against live fills.

One JSON object per line:
    {"ts": ISO8601, "symbol": "XSP 20260617 753 P",
     "bid": float|null, "ask": float|null, "last": float|null,
     "delta": float|null, "underlying_mid": float|null}

Best-effort: persistence must never break quote serving, so every failure is
swallowed (mirrors BarBuilder._persist).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")


class ChainSnapshotWriter:
    """Throttled append-only writer for option-chain quotes."""

    def __init__(self, persist_dir: str = "data/chains", throttle_seconds: float = 5.0) -> None:
        self._dir = Path(persist_dir)
        self._throttle = throttle_seconds
        # symbol -> monotonic seconds of last write (caller supplies the clock
        # so it matches get_quotes' event-loop time).
        self._last_write: dict[str, float] = {}

    def record(self, quotes: dict, now_mono: float, underlying_symbol: str = "XSP") -> None:
        """Append throttled option-quote rows from a get_quotes result dict.

        `quotes` maps symbol -> QuoteSnapshot. Only option symbols (those with
        a space, e.g. "XSP 20260617 753 P") are persisted; the underlying is
        already captured as bars. `now_mono` is a monotonic clock (seconds).
        """
        try:
            u = quotes.get(underlying_symbol)
            u_mid = None
            if u is not None:
                if u.bid is not None and u.ask is not None:
                    u_mid = (u.bid + u.ask) / 2.0
                else:
                    u_mid = u.last or u.close

            rows = []
            file_day = None
            for sym, q in quotes.items():
                if " " not in sym:
                    continue  # underlying / non-option
                if now_mono - self._last_write.get(sym, 0.0) < self._throttle:
                    continue
                self._last_write[sym] = now_mono
                ts = q.timestamp or datetime.now(UTC)
                if file_day is None:
                    file_day = ts.astimezone(_NY).strftime("%Y%m%d")
                rows.append({
                    "ts": ts.isoformat(),
                    "symbol": sym,
                    "bid": q.bid,
                    "ask": q.ask,
                    "last": q.last,
                    "delta": getattr(q, "delta", None),
                    "underlying_mid": round(u_mid, 4) if u_mid is not None else None,
                })

            if not rows:
                return
            self._dir.mkdir(parents=True, exist_ok=True)
            fname = self._dir / f"chain_{file_day}.jsonl"
            with open(fname, "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        except Exception:  # never let persistence break quote serving
            pass
