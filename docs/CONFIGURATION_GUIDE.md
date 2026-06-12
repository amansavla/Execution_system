# Configuration Guide

Complete reference for all configuration files and options.

## File Structure

```
configs/
├── broker.yaml              # IBKR connection + order execution settings
├── risk.yaml               # Position size limits and margin rules
├── strategies.yaml         # Strategy enablement and parameters
├── strategy_overrides.yaml # Per-strategy tuning (overrides strategies.yaml)
├── dashboard.yaml          # Dashboard settings
└── symbols.yaml            # Market data subscriptions
```

---

## broker.yaml — IBKR Connection & Order Settings

### Connection

```yaml
broker:
    host: 127.0.0.1            # TWS machine IP (local machine)
    port: 7497                 # Port for paper trading (7496 is live)
    client_id: 9               # Unique client ID for this process
```

**Setup:** Start TWS, enable API connections: Edit → Preferences → API → Settings → Enable ActiveX and Socket Clients.

### Order Repricing

```yaml
    order_repricing:
        cadence_seconds: 2.0           # Check quotes every 2s
        use_in_place_modify: true      # Modify in-place (vs. cancel/replace)
        min_reprice_threshold: 0.01    # Min price change to reprice ($0.01)
        
        # Quote staleness gate (prevents using stale data for SL)
        max_quote_staleness_seconds: 5.0
```

**Why `use_in_place_modify: true`?**
- Single IBKR message (vs. cancel + new order = 2 messages)
- Instant execution (no 11-31s Adaptive cancel wait)
- Order stays at best working price during reprice
- Especially critical for 0DTE options (premium moves fast)

### Adaptive Algorithm

```yaml
    # Disabled due to slow cancel confirmation (11-31s)
    # Orders would expire waiting for Adaptive cancel ACKs
    adaptive_priority: null
```

**History:** Before 2026-06-12 fix, Adaptive was enabled. During RTH 06:35, 5 shakeout cycles + 1 10-lot straddle all died 0-fill because:
1. Adaptive cancel → wait 11-31s for IBKR ACK
2. 60s order TTL / 2s reprice cadence = 30 reprices max
3. If half the reprices wait 15s for cancel ACK, order expires before replacement submitted
4. Fix: Disabled Adaptive, use in-place modify instead

### Gap Fill Detection (For Later Enhancement)

```yaml
    gap_fill:
        enabled: false              # Future: gap-fill detection on gaps
        min_gap_bps: 100           # Trigger on 100bps+ gap
```

---

## risk.yaml — Position Sizing & Limits

### Global Limits

```yaml
global:
    # Max contracts per single order submission
    max_contracts: 10
    
    # Max dollar premium per order (NEW in 2026-06-12 session)
    # Prevents runaway large orders
    max_premium_per_trade: 2000.0   # $2,000 max per trade
    
    # Max daily loss before system locks
    max_daily_loss: 10000.0
    
    # Position margin multiplier
    leverage: 12.0                  # Buying power multiplier
```

### Per-Strategy Overrides

```yaml
xsp_short_straddle:
    # Max single trade size
    max_contracts: 10
    
    # Risk as % of account per position
    position_sizing_pct: 0.025      # 2.5% risk per position
    
    # Use global leverage
    leverage: 12.0
```

### Premium Calculation Example

Given `max_premium_per_trade: 2000` (USD), what's the max order size if call costs $2.50 / contract?

```
per_contract_premium = $2.50 * 100 (option multiplier) = $250
max_qty_by_premium = $2000 / $250 = 8 contracts

Risk check also considers:
- max_contracts: 10                    → pass (8 < 10)
- position_sizing_pct: 0.025           → depends on account size
- Current open position margin         → depends on margin usage

Final allowed qty = min(8, 10, ...) = 8 contracts
```

**Before Fix (2026-06-12 12:35):**
- Config: `max_premium_per_trade: 2000`
- Order: 10-lot straddle @ $2.91/call = $2,910 premium
- Result: ✗ Submitted (budget exceeded)
- After: ✓ Clamped to 6 lots max

---

## strategies.yaml — Strategy Configuration

### Strategy Enablement

```yaml
strategies:
    xsp_short_straddle:
        enabled: true               # Enable/disable entire strategy
        
    xsp_breakout_0945:
        enabled: true
        
    dummy_test:
        enabled: false              # Disabled: test strategy only
```

### Per-Strategy Entry/Exit

```yaml
xsp_straddle_1100_40:
    enabled: true
    
    # ENTRY SETTINGS
    entry:
        entry_time: "11:00"         # Start trading at 11:00 AM ET
        entry_time_end: "11:05"     # Stop entry at 11:05 AM ET
        max_contracts: 10           # Max order size for this strat
        max_attempts: 3             # Max re-entries if first order rejects
    
    # EXIT SETTINGS
    exit:
        stop_loss_pct: 40.0         # Exit if down 40% of entry premium
        time_exit_utc: "15:30"      # Force close at 3:30 PM ET
        trailing_stop_pct: null     # Trailing stop disabled
    
    # POSITION SIZING
    position_sizing_pct: 0.025      # Risk 2.5% of portfolio
    leverage: 12.0                  # Use 12x leverage
    
    # RE-ENTRY CONTROL
    allow_reentry: true             # Can trade again same day
```

### Stop-Loss Calculation

`stop_loss_pct: 40.0` means:
- Entry: Sell straddle for $2.00 (call) + $1.50 (put) = $3.50 total
- Entry premium = $3.50 * 100 = $350 per contract
- Stop loss threshold = 40% * $350 = $140
- If current mark > $2.40 (= $140 + entry), exit triggered

```python
entry_premium = 3.50 * 100          # = $350
stop_threshold = 0.40 * entry_premium  # = $140
current_mark = 2.50
current_loss = (current_mark - entry_price) * 100  # = $50

if current_loss > stop_threshold:
    submit_exit()  # Triggered when loss > $140
```

### Time Exit

`time_exit_utc: "15:30"` = Close all positions at 3:30 PM ET (4:30 PM EDT in summer).

System converts to UTC internally. Example for ET (-4):
- ET: 15:30 = 3:30 PM Eastern
- UTC: 15:30 + 4 hours = 19:30

---

## strategy_overrides.yaml — Runtime Tuning

Overrides strategies.yaml on startup. Useful for quick tuning without code changes.

```yaml
xsp_straddle_1100_40:
    # Override enabled state (for live testing)
    enabled: true
    
    # Override entry time (start trading at 10:06 instead of 11:00)
    entry.entry_time: "10:06"
    
    # Override exit time
    exit.time_exit_utc: "15:30"
    
    # Override stop loss
    exit.stop_loss_pct: 40.0
    
    # Override position sizing
    position_sizing_pct: 0.025
    leverage: 12.0
    
    # NEW: Per-strategy re-entry control
    allow_reentry: true             # Allow re-entry same day
    
    # Max contracts override
    entry.max_contracts: 10
```

**Usage Pattern:**
```bash
# Run with all overrides
python3 -m src.app.runner configs/

# Later, disable a strategy for testing
# Edit strategy_overrides.yaml:
# dummy_test:
#   enabled: false
# 
# Changes take effect on next runner restart
```

---

## dashboard.yaml — Dashboard Settings

```yaml
dashboard:
    # Server settings
    host: 127.0.0.1
    port: 8500
    reload: true                    # Auto-reload on code changes
    
    # Database (shared with runner)
    db_path: data/events.db
    
    # Update cadence
    state_push_interval_seconds: 1.0  # WebSocket push every 1s
    
    # Data limits
    max_events: 50                  # Show last 50 events
    max_history: 100               # Show last 100 closed positions
    max_logs: 500                  # Show last 500 log lines
```

---

## symbols.yaml — Market Data Subscriptions

Defines which symbols and expirations the system subscribes to.

```yaml
symbols:
    XSP:
        expirations: ["20260612"]    # Subscribe to 6/12 expiry only
        # Later: "20260613", "20260614" for multi-day
        
    SPY:
        enabled: false               # Not used yet
```

---

## Environment Variables

### Runner

```bash
# Database path (defaults to data/events.db)
DASHBOARD_DB=data/events.db python3 -m src.app.runner configs/

# Runner log output (defaults to data/runner.log)
RUNNER_LOG=data/runner.log python3 -m src.app.runner configs/

# Logging level
LOG_LEVEL=DEBUG python3 -m src.app.runner configs/
```

### Dashboard

```bash
# Dashboard database (must match runner)
DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app

# Runner log for tail display
RUNNER_LOG=data/runner.log python3 -m uvicorn src.dashboard.app:app
```

---

## Configuration Best Practices

### For Paper Trading

```yaml
# broker.yaml
port: 7497              # Paper trading port

# risk.yaml
max_contracts: 5        # Small size (testing)
max_premium_per_trade: 500.0  # Low premium budget

# strategies.yaml
entry_time: "10:30"     # Morning only (less data noise)
time_exit_utc: "15:00"  # Close before market close
```

### For Live Trading (Future)

```yaml
# broker.yaml
port: 7496              # Live trading port (DIFFERENT!)
adaptive_priority: "Urgent"  # Use Adaptive for fills

# risk.yaml
max_contracts: 10
max_premium_per_trade: 5000.0  # Higher budget
max_daily_loss: 50000.0        # Stop on major loss

# strategies.yaml
# Extend hours, higher leverage, etc.
```

**CRITICAL:** Change `broker.port` from 7497 (paper) to 7496 (live) **ONLY** when going live. Easy mistake with big consequences.

---

## Configuration Load Order

1. **Default values** (hardcoded in code)
2. **strategies.yaml** (base strategy config)
3. **risk.yaml** (global + strategy overrides)
4. **broker.yaml** (IBKR settings)
5. **strategy_overrides.yaml** (runtime tuning) ← highest priority

This allows emergency fixes without restarting (edit strategy_overrides.yaml, restart runner).

---

## Validation on Startup

When runner starts, it validates:

```python
# Check required fields
assert config.broker.host is not None
assert config.broker.port in [7496, 7497]  # Live or paper only

# Check consistency
for strategy in config.strategies:
    assert strategy.entry.entry_time < strategy.exit.time_exit_utc
    assert strategy.exit.stop_loss_pct > 0
    assert strategy.position_sizing_pct > 0

# Check database
assert Path(config.db_path).parent.exists()

# Warn on suspicious values
if config.risk.max_contracts > 20:
    logger.warning("max_contracts > 20: unusually large")
if config.risk.leverage > 20:
    logger.warning("leverage > 20: very aggressive")
```

If validation fails, runner exits with clear error message (see logs).

---

## Quick Tweaking Examples

### Reduce Order Size for Testing

```yaml
# strategy_overrides.yaml
xsp_straddle_1100_40:
    entry.max_contracts: 1          # Test with 1 contract only
```

### Disable a Problematic Strategy

```yaml
xsp_breakout_0945:
    enabled: false                  # Skip this strategy
```

### Move Entry Time Earlier

```yaml
xsp_straddle_1100_40:
    entry.entry_time: "10:30"       # Start at 10:30 instead of 11:00
```

### Tighter Stop-Loss for Risk

```yaml
xsp_straddle_1100_40:
    exit.stop_loss_pct: 20.0        # Exit if down 20% (vs. 40%)
```

---

## Troubleshooting Config Issues

### Runner won't start

```bash
# Check YAML syntax
python3 -c "import yaml; yaml.safe_load(open('configs/broker.yaml'))"

# Check database exists
ls -la data/events.db

# Check TWS is running and API enabled
# TWS: Edit → Preferences → API → Settings ✓
```

### Dashboard shows "no_data"

```bash
# Runner must be running and writing events
tail -f data/runner.log | grep -i "event"

# Dashboard must point to same database
DASHBOARD_DB=data/events.db python3 -m uvicorn src.dashboard.app:app
```

### Orders not submitting

```bash
# Check risk config allows trades
# Check strategy is enabled in overrides
# Check time window (entry_time <= now <= entry_time_end)
# Check IBKR position limit not hit
```

### Stop-loss not triggering

```bash
# Check quote staleness (must be < 5s)
# Check stop_loss_pct value (40 means 40%, not 0.40)
# Check position is actually in market (not pending)
```
