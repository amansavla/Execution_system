"""Shadow replay: re-run the day's recorded bars through the NOTEBOOK
logic and diff against what the live system actually did.

This is the acceptance test for the fidelity revamp: if execution matches
the backtest, the per-trade diffs (signal time, entry/exit price, qty,
PnL) should be small and explainable (spread, queue). Large diffs mean a
fidelity bug.

Inputs:
  - data/bars/bars_YYYYMMDD.jsonl  (written live by BarBuilder)
  - data/events.db                 (signals, execution_quality fills)

Notebook semantics replayed (Long intraday / XSP 0DTE Grid Search.ipynb):
  - ref price = 09:30 bar close of the underlying
  - signal: first bar (>= entry scan time) whose close moves +/- trigger_pct
    from ref -> buy ATM CALL (up) / PUT (down), strike = round(underlying)
  - entry price = traded contract's next bar open after the signal bar
  - stop-loss: bar LOW <= entry*(1-SL%) -> exit at exactly the SL price
  - time exit: first bar at/after 15:20 NY -> exit at bar close

Straddle legs (short) replay exits only (per-leg SL on bar HIGH at
entry*(1+SL%), time exit 15:30) — entry strike selection needs the full
quote surface, which is not recorded as bars. Documented limitation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional

NY_EXIT_LONG = time(15, 20)
NY_EXIT_SHORT = time(15, 30)


@dataclass
class ReplayBar:
    symbol: str
    minute_ny: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class ReplayTrade:
    """What the notebook logic says SHOULD have happened."""
    strategy_id: str
    symbol: str               # option quote symbol
    direction: str            # long | short
    signal_minute: Optional[str] = None
    entry_price: Optional[float] = None
    exit_minute: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None


@dataclass
class TradeDiff:
    strategy_id: str
    symbol: str
    field_name: str
    replay_value: object
    actual_value: object
    delta: Optional[float] = None


def load_bars(path: str | Path) -> dict[str, list[ReplayBar]]:
    """Load a bars JSONL file -> {symbol: [bars sorted by minute]}."""
    out: dict[str, list[ReplayBar]] = {}
    p = Path(path)
    if not p.exists():
        return out
    with open(p) as f:
        for line in f:
            try:
                d = json.loads(line)
                bar = ReplayBar(
                    symbol=d["symbol"],
                    minute_ny=datetime.fromisoformat(d["minute_ny"]),
                    open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                )
                out.setdefault(bar.symbol, []).append(bar)
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
    for bars in out.values():
        bars.sort(key=lambda b: b.minute_ny)
    return out


def replay_breakout(
    underlying_bars: list[ReplayBar],
    option_bars: dict[str, list[ReplayBar]],
    strategy_id: str,
    entry_hour: int,
    entry_minute: int,
    trigger_pct: float,
    stop_loss_pct: float,
) -> Optional[ReplayTrade]:
    """Notebook long-breakout replay over recorded bars."""
    if not underlying_bars:
        return None

    ref = next((b.close for b in underlying_bars
                if b.minute_ny.time() == time(9, 30)), None)
    if ref is None:
        # fall back to first bar of the day (partial recording)
        ref = underlying_bars[0].close

    scan_from = time(entry_hour, entry_minute)
    signal_bar = None
    right = None
    for b in underlying_bars:
        if b.minute_ny.time() < scan_from:
            continue
        chg = (b.close - ref) / ref
        if chg >= trigger_pct:
            signal_bar, right = b, "C"
            break
        if chg <= -trigger_pct:
            signal_bar, right = b, "P"
            break
    if signal_bar is None:
        return None

    strike = float(round(signal_bar.close))
    # Find the traded option's bars: match by strike+right suffix in the
    # recorded symbols (e.g. 'XSP260611C00730000' or internal format).
    opt_sym, opt = None, None
    for sym, bars in option_bars.items():
        if _symbol_matches(sym, strike, right):
            opt_sym, opt = sym, bars
            break
    trade = ReplayTrade(strategy_id=strategy_id, symbol=opt_sym or f"XSP {strike} {right}",
                        direction="long",
                        signal_minute=signal_bar.minute_ny.isoformat())
    if not opt:
        return trade  # signal-only replay; no option bars recorded

    after = [b for b in opt if b.minute_ny >= signal_bar.minute_ny]
    if not after:
        return trade
    entry_price = after[0].open
    trade.entry_price = entry_price
    sl_price = entry_price * (1 - stop_loss_pct)

    for b in after:
        if b.low <= sl_price:
            trade.exit_minute = b.minute_ny.isoformat()
            trade.exit_price = sl_price
            trade.exit_reason = "StopLoss"
            return trade
        if b.minute_ny.time() >= NY_EXIT_LONG:
            trade.exit_minute = b.minute_ny.isoformat()
            trade.exit_price = b.close
            trade.exit_reason = "TimeExit"
            return trade
    trade.exit_minute = after[-1].minute_ny.isoformat()
    trade.exit_price = after[-1].close
    trade.exit_reason = "EndOfData"
    return trade


def replay_short_leg_exit(
    leg_bars: list[ReplayBar],
    entry_price: float,
    entry_minute: datetime,
    stop_loss_pct: float,
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """Notebook short-straddle per-leg exit replay (SL on bar HIGH)."""
    sl_price = entry_price * (1 + stop_loss_pct)
    for b in leg_bars:
        if b.minute_ny < entry_minute:
            continue
        if b.high >= sl_price:
            return b.minute_ny.isoformat(), sl_price, "StopLoss"
        if b.minute_ny.time() >= NY_EXIT_SHORT:
            return b.minute_ny.isoformat(), b.close, "TimeExit"
    if leg_bars:
        last = leg_bars[-1]
        return last.minute_ny.isoformat(), last.close, "EndOfData"
    return None, None, None


def _symbol_matches(symbol: str, strike: float, right: str) -> bool:
    s = symbol.upper().replace(" ", "")
    r = right.upper()[0]
    strike_int = int(strike)
    candidates = (
        f"{strike_int}{r}", f"{r}{strike_int}",
        f"{strike:.1f}{r}", f"{r}{strike:.1f}",
        f"{r}00{strike_int}000",  # OCC-style XSP260611C00730000
    )
    return any(c in s for c in candidates) or (str(strike_int) in s and r in s[-12:])


def load_actual_fills(db_path: str) -> list[dict]:
    """execution_quality events: actual fills with slippage data."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cur = conn.execute(
            "SELECT timestamp, payload FROM events "
            "WHERE event_type = 'execution_quality' ORDER BY timestamp ASC"
        )
        out = []
        for ts, payload in cur.fetchall():
            try:
                d = json.loads(payload)
                d["_timestamp"] = ts
                out.append(d)
            except json.JSONDecodeError:
                continue
        conn.close()
        return out
    except Exception:
        return []


def diff_trades(replay: ReplayTrade, fills: list[dict]) -> list[TradeDiff]:
    """Per-trade diff: replay expectation vs actual entry/exit fills."""
    diffs: list[TradeDiff] = []
    entries = [f for f in fills if f.get("is_entry") and f.get("strategy_id") == replay.strategy_id]
    exits = [f for f in fills if not f.get("is_entry") and f.get("strategy_id") == replay.strategy_id]

    if replay.entry_price is not None and entries:
        actual = entries[0]
        delta = round(actual["fill_price"] - replay.entry_price, 4)
        diffs.append(TradeDiff(replay.strategy_id, replay.symbol, "entry_price",
                               replay.entry_price, actual["fill_price"], delta))
        diffs.append(TradeDiff(replay.strategy_id, replay.symbol, "entry_time",
                               replay.signal_minute, actual["_timestamp"]))
    if replay.exit_price is not None and exits:
        actual = exits[-1]
        delta = round(actual["fill_price"] - replay.exit_price, 4)
        diffs.append(TradeDiff(replay.strategy_id, replay.symbol, "exit_price",
                               replay.exit_price, actual["fill_price"], delta))
        diffs.append(TradeDiff(replay.strategy_id, replay.symbol, "exit_time",
                               replay.exit_minute, actual["_timestamp"]))
        diffs.append(TradeDiff(replay.strategy_id, replay.symbol, "exit_reason",
                               replay.exit_reason, None))
    return diffs


def write_report(
    date_str: str,
    replays: list[ReplayTrade],
    all_diffs: list[TradeDiff],
    out_dir: str | Path = "reports",
    notes: Optional[list[str]] = None,
) -> tuple[Path, Path]:
    """Write reports/shadow_<date>.md + .diff.log; returns both paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = out / f"shadow_{date_str}.md"
    difflog = out / f"shadow_{date_str}.diff.log"

    lines = [f"# Shadow Replay Report — {date_str}", ""]
    lines.append(f"Replayed trades: {len(replays)}; diff rows: {len(all_diffs)}")
    lines.append("")
    for t in replays:
        lines += [
            f"## {t.strategy_id} — {t.symbol} ({t.direction})",
            f"- signal:     {t.signal_minute}",
            f"- entry:      {t.entry_price}",
            f"- exit:       {t.exit_price} @ {t.exit_minute} ({t.exit_reason})",
            "",
        ]
    if all_diffs:
        lines.append("## Per-trade diffs (replay vs actual)")
        lines.append("")
        lines.append("| strategy | symbol | field | replay | actual | delta |")
        lines.append("|---|---|---|---|---|---|")
        for d in all_diffs:
            lines.append(f"| {d.strategy_id} | {d.symbol} | {d.field_name} "
                         f"| {d.replay_value} | {d.actual_value} | {d.delta if d.delta is not None else ''} |")
    else:
        lines.append("_No actual fills to diff against._")
    if notes:
        lines += ["", "## Notes / limitations", ""] + [f"- {n}" for n in notes]
    report.write_text("\n".join(lines) + "\n")

    with open(difflog, "w") as f:
        for d in all_diffs:
            f.write(json.dumps({
                "strategy": d.strategy_id, "symbol": d.symbol,
                "field": d.field_name, "replay": str(d.replay_value),
                "actual": str(d.actual_value), "delta": d.delta,
            }) + "\n")
    return report, difflog
