# Changelog 2026-06-12

## Session Overview
Comprehensive fixes for asyncio task lifecycle, order repricing deadlocks, system lockup robustness, and dashboard functionality. All changes deployed to paper trading and validated through live testing.

---

## Core Fixes

### 1. **GC'd Asyncio Tasks — Repricers and Reconnect Freezing Orders** ⭐
**Commit:** `5c52588`

**Problem:**
- Exit/entry orders rested at initial limit for 120+ seconds unmanaged
- Background reprice tasks were being garbage-collected (asyncio only weak-references tasks)
- System became unresponsive to quote changes; orders timed out and got swept by cleanup

**Root Cause:**
```python
# ❌ Before: bare create_task — task eligible for GC
asyncio.create_task(self._reprice_order_loop(order_id, config))
```
Since no strong reference exists, Python GC can destroy the task before it completes.

**Solution:**
- Added `_background_tasks: set[asyncio.Task]` to hold strong references
- Created `_spawn()` method in OrderManager to manage task lifecycle
- Registered `add_done_callback` to remove task from set when it completes
- Applied to: repricers, cancel commands, stop orders, broker reconnect

**Code Changes:**
- `src/execution/order_manager.py`: Added `_spawn()` + `_background_tasks` set
- `src/broker/ibkr_broker.py`: Store `_reconnect_task` instead of bare create_task
- Applied to cancel order, reprice submit, cancel-wait loops

**Impact:** Orders now repriced consistently until timeout; no more unmanaged resting.

---

### 2. **Adaptive Algo + Cancel/Replace Repricing Self-Defeats** ⭐
**Commit:** `744abea`

**Problem:**
- RTH open: 5 shakeout cycles + 1 10-lot straddle all died 0-fill at first limit
- Adaptive algorithm cancels were taking 11–31 seconds to confirm
- 2s reprice cadence burned entire 60s timeout waiting for cancel ACKs
- Replacement orders never submitted before order expired

**Root Cause:**
```python
# ❌ Adaptive cancels slow: 11-31s to confirm
# This alone consumes 60s timeout on a 2s reprice cadence
```

**Solution:**
- Disabled Adaptive (`adaptive_priority: null` in `configs/broker.yaml`)
- Enabled in-place modify: single IBKR message, instant (no cancel wait)
- Bounded cancel-wait loop by order timeout instead of fixed 30s

**Code Changes:**
- `src/execution/order_manager.py`: Cancel-wait loop now respects `order.time_to_live`
- `src/app/runner.py`: Modified entry/exit reprice to use `use_in_place_modify=self._default_algo is None`
- `configs/broker.yaml`: Set `adaptive_priority: null` with explanation

**Validation:**
- 6 consecutive reprices at exact 2s cadence observed live (06:35–06:45 RTH)
- No cancels; all modifies instant

**Impact:** Orders repriced aggressively; fills now occur within seconds of entry, not 60s+ delays.

---

### 3. **Phantom Reconciliation Lock — Logged But Never Engaged** ⭐
**Commit:** `5f50384`

**Problem:**
- Reconciliation mismatch logged but system continued accepting new orders
- Dashboard showed "locked" but entries kept firing 4+ seconds later
- No actual lock gate preventing new orders

**Root Cause:**
```python
# ❌ Before: only logged, never locked
logger.warning("Reconciliation failed...")
# Missing: self.override_manager.state.system_locked = True
```

**Solution:**
- Added actual lock engagement on reconciliation failure
- Lock prevents new order submissions until cleared manually
- Operator can clear via dashboard or direct override

**Code Changes:**
- `src/app/runner.py`: Added `self.override_manager.state.system_locked = True` in `_reconciliation_failed()` callback

**Impact:** Reconciliation mismatches now halt trading to prevent cascading errors.

---

### 4. **Orphaned Orders After Shutdown — Overnight Fill Surprise** ⭐
**Commit:** `5f50384`

**Problem:**
- System shutdown didn't cancel working orders
- One order filled overnight while system was down
- On restart: untracked position caused SEVERE reconciliation mismatch

**Root Cause:**
```python
# ❌ Before: no order cleanup
async def _shutdown(self):
    await self._broker.disconnect()
    # Working orders still live on IBKR
```

**Solution:**
- Added active order sweep in `_shutdown()` before disconnect
- Cancels all working orders; waits up to 5s for ACKs
- Times out gracefully if ACKs don't arrive

**Code Changes:**
- `src/app/runner.py`: Added loop in `_shutdown()` to cancel working orders with 5s timeout

**Impact:** Clean shutdown; no orphaned orders surprise on restart.

---

### 5. **Runaway Exit Loop — "Cannot Have Open Orders on Both Sides"** ⭐
**Commit:** `5f50384`

**Problem:**
- Exit order rejected: "Cannot have open orders on both sides of same US option"
- Rejected exit became terminal; re-eligible every tick
- 14 rejected exits in 14 seconds (runaway loop)
- No backoff; no operator alerting

**Root Cause:**
- Orphaned entry order blocked exit submission
- Rejection handler didn't implement backoff

**Solution:**
- **Pre-exit opposite-side cancel:** Before submitting exit, scan for working BUY/SELL on same contract and cancel
- **Exit retry backoff:**
  - First failure: wait 10s before retry
  - After 5 failures: wait 60s + operator alert
  - Continues attempting once per minute

**Code Changes:**
- `src/app/runner.py`:
  - Added `_exit_attempts: dict[UUID, tuple[int, datetime]]` to track retries per position
  - Added `_cancel_opposite_side_orders()` called before exit submission
  - Added backoff logic in exit failure handler

**Impact:** Exits no longer spin in runaway loops; operator can see stuck exits via alert and logs.

---

### 6. **max_premium_per_trade Not Enforced** ⭐
**Commit:** `744abea`

**Problem:**
- Risk config had `max_premium_per_trade: 2000` (USD)
- 10-lot straddle fired at $291/contract = $29,100 total vs. $2,000 budget
- Config existed but never checked in risk engine

**Root Cause:**
```python
# ❌ Before: config ignored
qty = self._compute_allowed_quantity_by_contracts(...)
# max_premium_per_trade never checked
```

**Solution:**
- Added premium clamp in `_compute_allowed_quantity()`
- Calculates per-contract premium: `limit_price * 100.0` (option multiplier)
- If `qty * per_contract > max_premium`, reduce qty

**Code Changes:**
- `src/risk/risk_engine.py`: Added premium check + clamp

**Example:**
```python
max_premium = getattr(self._risk.global_, "max_premium_per_trade", None)
if max_premium and signal.limit_price:
    per_contract = abs(signal.limit_price) * 100.0
    if per_contract > 0:
        premium_qty = int(max_premium / per_contract)
        if premium_qty < qty:
            qty = premium_qty
            warnings.append(f"quantity_clamped_by_max_premium: {qty} contracts")
```

**Impact:** Large premium orders now clamped by risk; prevents runaway 10+ lot single trades.

---

## Dashboard Improvements

### 7. **Daily PnL Summary + Per-Strategy Real-Time** ⭐
**Commit:** `a953552`

**Problem:**
- No daily PnL breakdown
- Per-strategy PnL hidden in a data table
- No way to track today's performance vs. all-time

**Solution:**
- Added `daily_pnl_summary(days=30)` in PositionStore
- Buckets closed positions by America/New_York calendar date
- Computes realized + unrealized per strategy + per day
- Dashboard displays: today card, per-day table, per-strategy breakdown

**Code Changes:**
- `src/storage/position_store.py`: New `daily_pnl_summary()` method
- `src/dashboard/app.py`: Updated `/api/pnl` endpoint to return `{"today": {...}, "days": [...]}`
- `src/dashboard/static/index.html`: Added PnL tab with daily/per-strategy tables

**UI Layout:**
- Live tab: Compact 1-line PnL strip (summary only)
- PnL tab: 
  - Summary cards (total, today, per-strategy)
  - Per-strategy table (realized all-time, realized today, unrealized, total)
  - Daily breakdown (last 30 days)
  - Closed positions history (with close reason)

**Impact:** Operator can see daily/weekly trends at a glance; strategy performance tracking.

---

### 8. **Strategy Re-Entry Control Switch** ⭐
**Commit:** `a953552`

**Problem:**
- No way to block a strategy from re-entering after daily close
- Straddle v. long conflicts would cause re-entry fighting

**Solution:**
- Added `allow_reentry` boolean per strategy in runtime state
- Dashboard toggle to enable/disable per-strategy re-entry
- `_runtime_snapshot_dict()` includes `allow_reentry` and `traded_today`

**Code Changes:**
- `src/app/runner.py`: Modified snapshot to include per-strategy re-entry flag
- `src/dashboard/static/index.html`: Added toggle in strategy row

**Impact:** Manual control over which strategies can re-enter during same day.

---

### 9. **Dashboard Cache Stale Page on Update** ⭐
**Commit:** `fe8897c`

**Problem:**
- After UI update (new PnL tab, closed positions), browser served stale cached index.html
- New features appeared "missing" until hard refresh (Cmd+Shift+R)
- User confusing and bad UX

**Root Cause:**
```python
# ❌ Before: default cache headers
return FileResponse(STATIC_DIR / "index.html")
# Browser caches indefinitely
```

**Solution:**
- Added `Cache-Control: no-cache, must-revalidate` header to index.html route
- Forces browser to revalidate on every load; serves fresh page if changed

**Code Changes:**
- `src/dashboard/app.py`: Modified index route to include cache header

```python
return FileResponse(
    STATIC_DIR / "index.html",
    headers={"Cache-Control": "no-cache, must-revalidate"},
)
```

**Impact:** UI updates appear immediately without user intervention.

---

## Data Persistence

### 10. **Closed Position Attribution Persistence**
**Commit:** a953552

**Problem:**
- Closed positions lost on process restart (in-memory only)
- PnL history didn't survive restarts

**Solution:**
- `position_attribution` table now stores `realized_pnl`, `close_reason`, `closed_at` columns
- Auto-migrated on startup (forward migration for old DBs)
- Dashboard history queries check `status='CLOSED'`

**Code Changes:**
- `src/storage/position_store.py`: Added migration for new columns; updated upsert logic
- `src/dashboard/app.py`: `/api/history` reads closed positions with close reason

**Impact:** Multi-day PnL and close reason history now survives restarts.

---

## Testing & Validation

### Live Validation (RTH 2026-06-12 06:35–06:45)
- ✅ 6 consecutive reprices at exact 2s cadence (no Adaptive delays)
- ✅ Position entry <3s after order submission
- ✅ 2 straddle cycles seeded + closed without system intervention
- ✅ Dashboard updated in real-time (WebSocket 1s cadence)
- ✅ PnL reflected within 2s of close
- ✅ Close reasons (time exit, manual close) tracked correctly

### Restart Validation
- ✅ Orphaned order cleanup on shutdown
- ✅ Position re-seeding from broker on startup
- ✅ PnL history persisted across restart
- ✅ Per-strategy `allow_reentry` state maintained

### Unit & Integration Tests
- ✅ All 476 tests passing (src/tests/)
- ✅ No new test failures introduced

---

## Configuration Changes

### `configs/broker.yaml`
- `adaptive_priority: null` — Disabled Adaptive; switched to in-place modify repricing

### `configs/overrides.yaml`
- No permanent changes (testing-only overrides applied during live test)

---

## Commits Summary

| Commit | Title | Category |
|--------|-------|----------|
| 5c52588 | Fix GC'd asyncio tasks | Core stability |
| 744abea | RTH fill fix + premium enforcement | Order execution |
| 5f50384 | Restart safety + exit backoff | System robustness |
| a953552 | Dashboard PnL + re-entry control | UI/UX |
| fe8897c | Dashboard cache header | UI/UX |

---

## Known Limitations & Future Work

1. **IBKR Paper Trading Fill Rates**
   - Paper trading fills are slower/less reliable than live
   - This is expected and not a bug (confirmed with user)
   - Production deployments may see faster fills

2. **Reconciliation Lock Manual Clear**
   - Currently no automatic unlock timer
   - Operator must manually clear via dashboard or code
   - Consider auto-clear on next successful recon in future

3. **Dashboard Performance**
   - WebSocket state updates at 1s cadence (acceptable)
   - Event table queries limited to last 50 events (prevents DB overload)
   - Consider indexing on timestamp if dashboard grows

4. **Position Re-seeding Edge Case**
   - If multiple positions open on same contract with different strategies, only most recent is re-seeded
   - Unlikely in practice (0DTE options don't persist overnight)
   - Could improve by storing full position chain if needed

---

## Deployment Notes

### For Production
1. Test with live orders on small sizes (1-2 lot)
2. Monitor exit backoff alerts for any position stuck >5 attempts
3. Verify Adaptive remains disabled or reconfigure if IBKR improves cancel timing
4. Keep reconciliation alerts visible on operator dashboard

### Operator Runbook
- See `docs/RUNBOOK.md` for manual intervention procedures
- See `docs/INCIDENTS_2026-06-11.md` for incident post-mortem

---

## Session Statistics

- **Duration:** 2026-06-11 15:00 — 2026-06-12 10:00 (live market + GTH analysis)
- **Issues Identified:** 6 critical system bugs
- **Issues Fixed:** 6/6 (100%)
- **Lines Changed:** ~500 (core + dashboard)
- **Tests Added:** 0 (all existing tests passing)
- **Live Test Hours:** 3.5h (GTH + RTH)
