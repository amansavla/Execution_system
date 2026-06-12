# Incident review & fixes — 2026-06-11

Deep review of the live paper sessions of 2026-06-10/11 (570MB
`runner.log`, `events.db`, `position_attribution`). Each item lists the
observed failure, root cause, and the fix now in place.

## 1. Straddle / long entry legs being cancelled ("order cancel issue")

**Observed**: straddle entries placed at 13:35:47 were cancelled at
13:35:59; messages like *"Peer entry leg … failed/canceling (status:
OrderStatus.CANCEL_PENDING). Canceling active peer leg …"*; one leg
filled, the other killed → asymmetric position flattened at a loss.

**Root cause**: the repricer routinely cancel/replaces orders every few
seconds. The old multi-leg coordination treated ANY cancelled (or even
CANCEL_PENDING — i.e. mid-reprice) entry order as a *failed leg* and
hard-cancelled its healthy peers.

**Fix**: replacement-chain-aware coordination (commit `9934bfe`). Each
replacement links `old.superseded_by = new`. A leg has failed only when
the FINAL order of its chain is terminal (CANCELLED/REJECTED/ERROR) with
zero fill. CANCEL_PENDING is never a failure signal.

## 2. Orders stuck in CANCEL_PENDING (cancel confirmations lost)

**Observed**: *"Reprice timed out after 183.6 seconds"* with the order
still CANCEL_PENDING; legs sat in limbo — invisible to the entry-timeout
sweep (which only looks at working statuses) and deliberately ignored by
leg coordination.

**Fix**: `OrderManager.resolve_stuck_cancels()` runs every tick. Orders
in CANCEL_PENDING for >20s are checked against the broker's ground truth
(`IBKRBrokerClient.get_order_status` over the trades cache): terminal →
adopt the status; unknown to broker → mark CANCELLED; still working →
re-issue the cancel.

## 3. Entries fired outside the strategy's window

**Observed**: after a 15:46 restart, straddles re-entered (their 15:30
time exit already past); strategies kept emitting signals all evening,
spamming warnings every second after the market closed.

**Root causes**: (a) entry condition was simply `now >= entry_time`;
(b) the one-trade-per-day set was in-memory and lost on restart; (c) a
risk-rejected batch retried every tick forever.

**Fixes** (all in the runner, so they cover every strategy):
- entry polls are skipped once `now >= exit.time_exit_utc` (NY time);
- a persistent traded-today set is loaded from `position_attribution`
  on startup (`PositionStore.strategies_traded_on`) and updated on fills;
- a 30-second back-off follows any risk-rejected signal batch.

## 4. `database is locked` → dashboard PnL/close reasons lost

**Observed**: `PositionStore upsert failed … database is locked` exactly
when positions closed; the dashboard's all-time PnL and history showed
$0 / blank reasons everywhere.

**Root cause**: the EventStore writer committed once per event,
monopolising the WAL write lock during bursts; the close-time
attribution upsert gave up after one attempt.

**Fixes**: batched event inserts (single transaction per ≤100 queued
events), retrying upserts (3 attempts, backoff, off-loop via
`asyncio.to_thread`), threadpooled dashboard endpoints (`def` instead of
`async def` so blocking sqlite cannot stall the UI event loop), cached
stores. `scripts/backfill_pnl.py` repaired historical rows from
`position_closed` events.

## 5. 570 MB/day log, every line twice

**Root cause**: global DEBUG level (ib_async wire chatter was 94% of
lines) + double sink (FileHandler *and* StreamHandler whose stdout the
supervisor redirected into the same file).

**Fix**: INFO base level, WARNING for `ib_async.*`/`aiosqlite`, single
`RotatingFileHandler` (50 MB × 5 backups), per-tick log line demoted to
DEBUG, straddle no-quote warning rate-limited to once/60s.

## 6. Stop-loss checks skipped on live positions (slippage source)

**Observed**: *"skipping stop/target evaluation … quote_stale:
age=5.5s"* and *"spread_too_wide: 16.8%"* streaks while a short call ran
against the position.

**Root cause**: streaming option tickers only refresh their timestamp
when the NBBO changes, so quiet contracts look stale within seconds; 15%
spread is routine for late-day 0DTE. Skipping stop evaluation left
positions unprotected — strictly worse than evaluating an unchanged NBBO.

**Fix**: exits now use `quote_freshness.exit_max_age_seconds` (30s,
entries keep 5s) and tolerate 2× the entry spread limit.

## 7. Faster exit fills (slippage in trending markets)

Exit repricer now chases every 1s (was 2s) and, from the 3rd attempt,
prices THROUGH the touch by $0.05 (marketable-plus limit — fills like a
protected market order, still bounded so far-outside-NBBO rejections
can't happen). Entries keep the conservative inside-NBBO ladder.
IBKR Adaptive (Urgent) stays on for RTH single-leg orders. NOTE: IBKR
rejects in-place modification of Adaptive orders — `use_in_place_modify`
must stay False.

## 8. Restart hygiene

- Seeded positions now adopt their persisted `position_id` (no more
  duplicate OPEN attribution rows per restart).
- IBKR zero-quantity position rows are no longer seeded as OPEN.
- A strategy-provider exception no longer aborts the whole tick (exit
  checks and the dashboard snapshot always run).

## 9. GTH live findings (20:15+ ET session, XSP next-day 0DTE)

XSP/SPX/VIX trade Cboe GTH 20:15–09:15 ET (IBKR needs FOP permissions);
orders must carry `outsideRth=True` off-RTH or IBKR queues them for the
next RTH open. Cycle results:

- **Cycle 1 — clean end-to-end**: both legs filled via the repricer
  (2.3s / 0.1s, 3–6¢ slippage vs first limit), 180s hold, time exits
  filled in 0.12–0.18s at zero slippage, `close_reason=time_exit` and
  realized PnL persisted to the dashboard.
- **Cycle 2 — fail-safe**: batch risk-rejected on a stale overnight
  quote (entry gate, 5s); the 30s back-off held, next cycle recovered.
- **Cycle 3 — found and fixed a real bug**: the call leg's cancel
  confirmation was LOST (the new stuck-cancel sweep resolved it in 20s —
  verified live), the leg died 0-filled, but the filled put was never
  flattened: `Position.entry_order_id` carried the broker-layer fill
  UUID, which OrderManager never issued, so the peer-flatten lookup
  matched nothing. Fixed by remapping fill ids to OrderManager orders in
  the runner's fill handler + a contract-identity fallback.
- **Iteration 2 — found and fixed a second bug**: after one manually
  cancelled leg, the strategy/day grouping kept matching that dead leg
  against every later cycle, hard-canceling fresh legs and flattening
  fresh fills for the rest of the session (observed live across cycles
  spanning 30+ minutes). Coordination is now scoped to an `entry_batch`
  tag (uuid per approved signal batch, carried through replacement
  chains). The asymmetric peer-flatten itself was verified firing live
  (position flagged → strategy_exit submitted → flat).
- Overnight caveat: far-OTM books go one-sided late evening; entries are
  risk-rejected with `quote_incomplete:missing_bid_or_ask` until the
  book two-sides again (fail-safe, auto-retries).

## Validation

- Full suite: `python3 -m pytest tests/unit tests/integration -q
  --ignore=tests/integration/test_ibkr_paper_connection.py`
- Off-hours broker validation: `python3 scripts/live_cancel_shakeout.py`
  (client_id 17; deep-OTM $0.01 limits that cannot fill; asserts
  place→ack, cancel→confirm, cancel/replace chain, ground-truth query).
- During RTH: enable `shakeout_cycle_test` in `configs/strategies.yaml`
  for full entry→exit cycling through the real pipeline.
