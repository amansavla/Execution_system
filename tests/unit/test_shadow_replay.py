"""Phase 6 unit tests: shadow replay reproduces notebook semantics.

Run: python3 -m pytest tests/unit/test_shadow_replay.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.replay.shadow import (
    ReplayBar,
    diff_trades,
    load_bars,
    replay_breakout,
    replay_short_leg_exit,
    write_report,
)

_NY = ZoneInfo("America/New_York")


def _bar(symbol: str, h: int, m: int, o: float, hi: float, lo: float, c: float) -> ReplayBar:
    return ReplayBar(symbol=symbol,
                     minute_ny=datetime(2026, 6, 11, h, m, tzinfo=_NY),
                     open=o, high=hi, low=lo, close=c)


def _flat_underlying(ref: float, until_h: int, until_m: int) -> list[ReplayBar]:
    """ref close at 9:30, flat until the breakout bar."""
    bars = [_bar("XSP", 9, 30, ref, ref, ref, ref)]
    t = datetime(2026, 6, 11, 9, 31, tzinfo=_NY)
    end = datetime(2026, 6, 11, until_h, until_m, tzinfo=_NY)
    while t < end:
        bars.append(_bar("XSP", t.hour, t.minute, ref, ref, ref, ref))
        t += timedelta(minutes=1)
    return bars


class TestBreakoutReplay:
    def test_signal_on_bar_close_cross_and_stop_loss_exit(self) -> None:
        ref = 700.0
        under = _flat_underlying(ref, 10, 0)
        # 10:00 bar closes +0.25% -> CALL signal, ATM strike = round(701.75)=702
        under.append(_bar("XSP", 10, 0, ref, 702.0, ref, 701.75))

        opt = "XSP260611C00702000"
        option_bars = {opt: [
            _bar(opt, 10, 0, 2.00, 2.10, 1.90, 2.05),   # entry bar: open 2.00
            _bar(opt, 10, 1, 2.05, 2.05, 1.35, 1.50),   # low 1.35 <= SL 1.40
        ]}

        t = replay_breakout(under, option_bars, "xsp_0dte_1000",
                            10, 0, 0.002, 0.30)
        assert t is not None
        assert t.signal_minute.endswith("10:00:00-04:00")
        assert t.entry_price == 2.00
        assert t.exit_reason == "StopLoss"
        assert t.exit_price == 2.00 * 0.70  # exit at exactly the SL price

    def test_time_exit_at_1520(self) -> None:
        ref = 700.0
        under = _flat_underlying(ref, 10, 0)
        under.append(_bar("XSP", 10, 0, ref, ref, 697.0, 698.0))  # -0.286% -> PUT

        opt = "XSP260611P00698000"
        bars = [_bar(opt, 10, 0, 3.00, 3.10, 2.95, 3.05)]
        # no SL hit; runs to 15:20
        bars.append(_bar(opt, 15, 19, 3.0, 3.1, 2.9, 3.0))
        bars.append(_bar(opt, 15, 20, 2.8, 2.9, 2.7, 2.85))
        t = replay_breakout(under, {opt: bars}, "xsp_0dte_1000",
                            10, 0, 0.002, 0.30)
        assert t.exit_reason == "TimeExit"
        assert t.exit_price == 2.85  # bar close at 15:20

    def test_no_signal_when_flat(self) -> None:
        under = _flat_underlying(700.0, 15, 0)
        t = replay_breakout(under, {}, "xsp_0dte_1000", 10, 0, 0.002, 0.30)
        assert t is None


class TestShortLegReplay:
    def test_short_leg_sl_on_bar_high(self) -> None:
        opt = "XSP260611C00702000"
        entry_minute = datetime(2026, 6, 11, 10, 0, tzinfo=_NY)
        bars = [
            _bar(opt, 10, 0, 0.40, 0.42, 0.38, 0.41),
            _bar(opt, 10, 1, 0.41, 0.49, 0.40, 0.45),  # high 0.49 >= 0.48 SL
        ]
        minute, price, reason = replay_short_leg_exit(bars, 0.40, entry_minute, 0.20)
        assert reason == "StopLoss"
        assert price == 0.40 * 1.20

    def test_short_leg_time_exit_1530(self) -> None:
        opt = "XSP260611P00698000"
        entry_minute = datetime(2026, 6, 11, 10, 0, tzinfo=_NY)
        bars = [
            _bar(opt, 10, 0, 0.40, 0.42, 0.38, 0.41),
            _bar(opt, 15, 30, 0.10, 0.12, 0.08, 0.09),
        ]
        minute, price, reason = replay_short_leg_exit(bars, 0.40, entry_minute, 0.20)
        assert reason == "TimeExit"
        assert price == 0.09


class TestDiffAndReport:
    def test_diff_and_report_files(self, tmp_path: Path) -> None:
        ref = 700.0
        under = _flat_underlying(ref, 10, 0)
        under.append(_bar("XSP", 10, 0, ref, 702.0, ref, 701.75))
        opt = "XSP260611C00702000"
        t = replay_breakout(under, {opt: [
            _bar(opt, 10, 0, 2.00, 2.10, 1.90, 2.05),
            _bar(opt, 10, 1, 2.05, 2.05, 1.35, 1.50),
        ]}, "xsp_0dte_1000", 10, 0, 0.002, 0.30)

        fills = [
            {"strategy_id": "xsp_0dte_1000", "is_entry": True,
             "fill_price": 2.05, "_timestamp": "2026-06-11T14:00:05+00:00"},
            {"strategy_id": "xsp_0dte_1000", "is_entry": False,
             "fill_price": 1.42, "_timestamp": "2026-06-11T14:01:10+00:00"},
        ]
        diffs = diff_trades(t, fills)
        by_field = {d.field_name: d for d in diffs}
        assert by_field["entry_price"].delta == 0.05    # paid 5c over bar open
        assert by_field["exit_price"].delta == 0.02     # 1.42 vs SL 1.40

        report, difflog = write_report("2026-06-11", [t], diffs, out_dir=tmp_path,
                                       notes=["test note"])
        assert report.exists() and difflog.exists()
        assert "entry_price" in report.read_text()
        logged = [json.loads(l) for l in difflog.read_text().splitlines()]
        assert any(d["field"] == "exit_price" for d in logged)

    def test_load_bars_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "bars_20260611.jsonl"
        f.write_text(json.dumps({
            "symbol": "XSP", "minute_ny": "2026-06-11T09:30:00-04:00",
            "open": 700.0, "high": 700.5, "low": 699.5, "close": 700.2,
            "ticks": 12,
        }) + "\n")
        bars = load_bars(f)
        assert len(bars["XSP"]) == 1
        assert bars["XSP"][0].close == 700.2
