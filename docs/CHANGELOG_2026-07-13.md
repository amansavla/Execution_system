# Changelog: sessions 2026-07-01 → 2026-07-13

Everything since commit `e12d756` (entry-window bound). Three live paper
sessions: 07-01 (feature work + shakeout), 07-02 (full RTH, two real bugs
found, one fixed live), 07-13 (green day, +$1,028, manual-fire button
shipped and validated live).

---

## Features

### Delta-target strike selection for straddles
- `entry.strike_selection: premium_target | delta_target` (default unchanged:
  `premium_target`), `entry.target_delta` (magnitude, e.g. `0.30`).
- Delta mode picks the strike whose |delta| is closest to target (call: `+d`,
  put: `-d`), using IBKR `modelGreeks.delta` already streaming on every
  option ticker. Strikes with no delta yet are skipped (falls through to
  next-closest), sizing math unchanged.
- Both fields live-editable via `update_strategy`. Signal metadata records
  `strike_selection`, `target_delta`, and achieved `option_delta` per leg.
- Validated live 07-01 (xsp_straddle_1330_30, both legs filled ATM).
- Files: `src/core/config.py`, `src/strategies/xsp_short_straddle.py`,
  `src/app/runner.py` (EDITABLE_STRATEGY_PARAMS).

### "Fire Straddle" dashboard button (manual straddle on demand)
- New `fire_straddle` command + `xsp_straddle_manual` strategy that never
  fires on its own (unreachable 23:59 entry_time); entries come only from
  the button. One click = one straddle; wait-until-flat spaces repeat
  clicks; usable 9:30–15:30 ET (rejected after the cutoff with a reason).
- Runner consumes the request on the next tick via the provider's new
  `emit_now()` (normal chain-scan/sizing, schedule gates skipped) — same
  signal → risk → order path as scheduled entries, no parallel route.
- Validated end-to-end live 07-13 15:16 ET through the exact button path
  (dashboard API → command queue → runner): filled 749C×6 + 755P×6, no errors.
- Files: `src/control/command_queue.py`, `src/app/runner.py`,
  `src/strategies/xsp_short_straddle.py`, `src/strategies/composite.py`,
  `configs/strategies.yaml`, `src/dashboard/static/index.html`,
  `docs/DASHBOARD_API.md`.

### Liquidity filter on straddle strike selection
- Strike candidates whose quote fails `validate_quote_prices` (spread >
  `risk.yaml spread_limits.max_spread_pct`, default 15%) are skipped at
  selection time. Prevents "closest premium to target" landing on a
  flickering, thinly-quoted strike that then chases forever (2026-06-17
  stuck-exit incident; part of the slippage investigation below).

## Bug fixes (each found in live paper trading)

### 1. Stale/frozen option `last` polluting 1-min bars → false stop-loss (07-02, FIXED same day)
- ib_async `pendingTickersEvent` fires on ANY field change; the handler
  re-fed `ticker.last` into the bar builder on every bid/ask tick even when
  `last` hadn't changed. A pre-entry print (4.06) frozen for 2+ minutes kept
  `bar.high` pinned above the stop threshold while live bid/ask was far
  below → primary bar-based stop check fired on a price that was never
  tradeable (XSP 742C, exit was +$75 by luck; trigger was bogus).
- Fix: per-symbol dedup — only feed `on_tick` when the price actually
  changed (`_last_bar_tick_price` in `src/broker/ibkr_broker.py`).
- Deployed live 13:08 ET on 07-02; verified real stops (748P) still fire on
  genuinely moving prints. Tests: `tests/unit/test_bar_stale_last_guard.py`.

### 2. Shared-contract seeding mis-attribution on restart (07-01, FIXED)
- Two strategies independently selected the identical contract (1100_40 and
  1300_30 both short XSP 747C). Broker reports one NET quantity per
  contract; startup seeding attributed the whole 12-lot to the
  most-recently-updated attribution row → internal ledger showed 18 tracked
  vs 12 real (double-exit risk).
- Fix: `find_all_open_attributions()` returns every OPEN row for the
  contract; seeding splits the broker position across them (each keeps its
  own qty/price/position_id) and logs an ERROR if the sum doesn't reconcile.
- Verified live on restart with 6 strategies sharing one 752P (07-13).
- Files: `src/storage/position_store.py`, `src/app/runner.py`.
  Tests: `tests/unit/test_position_store.py`.

### 3. Entry-window bound wrongly clamped breakout strategies (07-02, FIXED)
- The 5-min entry window (built to stop straddles re-firing late after a
  restart) keyed off the `_HHMM` token in the strategy id, so it also
  closed breakout strategies 5 min after their scan-START time — but
  breakouts watch continuously for a % trigger until their time_exit.
  xsp_0dte_1030/1100/1200 were blocked all morning on 07-02 with a live
  trigger condition.
- Fix: bound (b) now applies only to `signal_source == "xsp_short_straddle"`
  (`_TIME_TRIGGERED_SIGNAL_SOURCES`); exit-time bound (a) still applies to
  everyone. On deploy the three blocked strategies correctly entered.
- Also removed breakout's mark-traded-on-emit (a rejected signal no longer
  burns the daily slot — mirrors straddle fix 67a720b).
- Files: `src/app/runner.py`, `src/strategies/xsp_breakout.py`.
  Tests: `tests/unit/test_entry_window.py`.

### 4. Entries could chase without ever crossing (tuning)
- Entry RepriceConfig now sets `cross_touch_after_attempts=4` (mirrors
  exits) so a chasing entry can't spin its whole attempt budget and cancel
  unfilled.

## Slippage / time-to-fill investigation (the "orders take too long" question)

Verdict: it was **mostly our own logic, not the paper environment** — and
the fixes show up in the numbers:

| session | fills | p50 | p90 | max |
|---|---|---|---|---|
| 06-11 | 48 | 10.5s | 34.2s | 42s |
| 06-12 | 96 | 4.6s | 21.3s | 63s |
| 07-02 | 57 | 2.7s | 11.8s | 23s |
| 07-13 | 29 | 3.5s | 12.8s | 12.8s |

- Root causes found: (a) strike selection had no liquidity filter, so
  premium-targeting could pick a strike whose quote flickers with no real
  prints — the repricer then chases a phantom NBBO (IBKR paper generally
  needs a real print through the limit to fill); (b) entries started at mid
  and never crossed the touch. Both fixed (liquidity filter + cross-touch).
- Residual paper-environment artifact: IBKR paper fills need a trade print,
  so genuinely illiquid strikes will always fill slower in paper than a
  live MM would trade. This tail can't be tuned away in paper.
- Slippage remains small in absolute terms (avg ≈ $0.02–0.05/contract on
  $1–3.5 premiums); percentage figures look big only because premiums are small.

## Known open issues (priority order)

1. **In-place-modify repricer race (Error 104 → 201 → 202)** — the
   long-flagged top pre-live item, still unbuilt. When a modify lands just
   as the order fills (or after a cancel), IBKR rejects with "too late to
   replace"/"cannot modify filled order" and cancels the remainder.
   Recurred ~6× across 07-02/07-13. Never locked the system or orphaned a
   broker position, but it: undersized entries (1230_30 call 5/10,
   1330_30 put 1/6 on 07-02 → lopsided straddle) and left one exit 9/10
   done with a stale OPEN qty=1 attribution row (07-13, broker was
   actually flat; row corrected manually). Proper fix: reconcile
   fill-vs-modify race in the repricer (stop modifying once a fill event
   for the full remaining qty is in flight; on 201/202 re-check broker
   state and re-issue for true remainder) + NBBO-clamp on reprice prices.
2. **Partial-exit remainder not re-triggered** — when an exit order is
   cancelled with a remainder (per #1), ExitManager does not re-fire
   time_exit for the residual (07-13: 1 contract sat past its time_exit;
   turned out to be bookkeeping-only, but a real remainder would sit
   unmanaged until EOD).
3. **3 pre-existing 5EMA unit test failures**
   (`test_xsp_5_ema.py` signal-bar/entry cases) — fail on unmodified code,
   predate all of this work; strategy behaves correctly live (entered and
   stop-exited cleanly 07-02). Needs a test-expectation review.
4. **`test_dashboard_exit_identical_to_automated_exit` is flaky** under the
   full suite (passes alone and on re-run) — SQLite contention in tests.
5. **docs/DASHBOARD_API.md drift** — lists command types that don't exist
   (`close_all`, `enable_reentry`); actual set lives in
   `src/control/command_queue.py::VALID_COMMAND_TYPES`.
6. **07-01 EOD hang** — TWS went down mid-evening and the runner sat 19.5h
   silent on ConnectionRefused instead of exiting for the supervisor to
   handle; consider a reconnect-give-up → clean exit after N minutes.

## Operational notes
- Risk config: `max_open_positions` 10→20, `per_underlying.max_positions`
  5→15 (all strategies trade XSP; 5 starved the book).
- 07-02 session: daily loss limit ($5k) breached 14:57 ET and correctly
  blocked all new entries while exits kept working; day recovered from
  −$6.8k to −$4.4k final on the 15:20 time-exits.
- `xsp_late_*` re-entry loops (allow_reentry) repeatedly re-entered and
  re-stopped in the 07-02 chop — worth a per-strategy daily loss cap or
  re-entry cooldown.
