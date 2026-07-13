from datetime import UTC, datetime, date, time
from zoneinfo import ZoneInfo

from src.app.runner import ExecutionRunner
from src.core.config import StrategyConfig, StrategyEntryConfig, StrategyExitConfig


# _entry_window_closed/_strategy_entry_time_ny only touch
# self._TIME_TRIGGERED_SIGNAL_SOURCES and call each other — no broker, DB,
# or event loop needed, so a bare stand-in avoids ExecutionRunner's heavy
# constructor.
class _RunnerStub:
    _TIME_TRIGGERED_SIGNAL_SOURCES = ExecutionRunner._TIME_TRIGGERED_SIGNAL_SOURCES
    _strategy_entry_time_ny = ExecutionRunner._strategy_entry_time_ny
    _entry_window_closed = ExecutionRunner._entry_window_closed


def _ny_time(hh: int, mm: int) -> datetime:
    tz_ny = ZoneInfo("America/New_York")
    return datetime.combine(date(2026, 7, 2), time(hh, mm), tzinfo=tz_ny).astimezone(UTC)


def test_straddle_window_closes_after_5_minutes() -> None:
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_straddle_1230_30",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=10),
        exit=StrategyExitConfig(time_exit_utc="20:00"),  # far away, doesn't interfere
    )
    assert runner._entry_window_closed(cfg, _ny_time(12, 34)) is False   # within window
    assert runner._entry_window_closed(cfg, _ny_time(12, 36)) is True    # past window


def test_breakout_never_closes_on_entry_time_alone() -> None:
    # Regression 2026-07-02: xsp_0dte_1230 must be able to fire on a
    # threshold trigger hours after its "1230" scan-start name, not just
    # within a 5-minute window — the entry-window bound was built for
    # fixed-instant strategies (straddles), not continuous-watch breakouts.
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_0dte_1230",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_breakout", max_contracts=20),
        exit=StrategyExitConfig(time_exit_utc="20:00"),
    )
    assert runner._entry_window_closed(cfg, _ny_time(12, 34)) is False
    assert runner._entry_window_closed(cfg, _ny_time(15, 0)) is False  # 2.5h later, still open


def test_breakout_late_never_closes_on_entry_time_alone() -> None:
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_late_1330",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_breakout_late", max_contracts=20),
        exit=StrategyExitConfig(time_exit_utc="20:00"),
    )
    assert runner._entry_window_closed(cfg, _ny_time(15, 30)) is False


def test_unknown_signal_source_exempt_like_5ema() -> None:
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_5ema_base",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_5_ema", max_contracts=10),
        exit=StrategyExitConfig(time_exit_utc="20:00"),
    )
    assert runner._entry_window_closed(cfg, _ny_time(15, 0)) is False


def test_exit_time_bound_applies_to_all_signal_sources() -> None:
    # Bound (a) — once past time_exit, blocked regardless of type — must
    # still apply even to exempt (breakout/5ema) strategies.
    runner = _RunnerStub()
    for source in ("xsp_breakout", "xsp_breakout_late", "xsp_5_ema", "xsp_short_straddle"):
        cfg = StrategyConfig(
            strategy_id="xsp_test_1230",
            enabled=True,
            underlying="XSP",
            entry=StrategyEntryConfig(signal_source=source, max_contracts=10),
            exit=StrategyExitConfig(time_exit_utc="15:20"),
        )
        assert runner._entry_window_closed(cfg, _ny_time(15, 25)) is True, source


def test_explicit_entry_time_override_still_gated_by_signal_source() -> None:
    # An explicit entry.entry_time on a breakout strategy must NOT trigger
    # the window bound either — only signal_source decides.
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_0dte_custom",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_breakout", max_contracts=20,
                                   entry_time="10:00"),
        exit=StrategyExitConfig(time_exit_utc="20:00"),
    )
    assert runner._entry_window_closed(cfg, _ny_time(14, 0)) is False


def test_straddle_explicit_entry_time_override_is_gated() -> None:
    runner = _RunnerStub()
    cfg = StrategyConfig(
        strategy_id="xsp_straddle_custom",
        enabled=True,
        underlying="XSP",
        entry=StrategyEntryConfig(signal_source="xsp_short_straddle", max_contracts=10,
                                   entry_time="09:42"),
        exit=StrategyExitConfig(time_exit_utc="20:00"),
    )
    assert runner._entry_window_closed(cfg, _ny_time(9, 46)) is False
    assert runner._entry_window_closed(cfg, _ny_time(9, 48)) is True
