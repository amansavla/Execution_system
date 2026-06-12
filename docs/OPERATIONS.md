# Operations & Deployment Guide

Production deployment, monitoring, incident response, and operational procedures.

---

## Deployment

### Paper Trading (Development / Testing)

```bash
# Terminal 1: Runner
python3 -m src.app.runner configs/

# Terminal 2: Dashboard
python3 -m uvicorn src.dashboard.app:app --port 8500

# Terminal 3: Monitor logs
tail -f data/runner.log
```

**Requirements:**
- TWS running locally (or on accessible machine)
- Paper trading account enabled
- API enabled in TWS (Edit → Preferences → API)
- Port 7497 available

### Live Trading (Future)

**CRITICAL CHANGES:**

1. **Broker port: 7497 → 7496**
   ```yaml
   # configs/broker.yaml
   broker:
       port: 7496  # ⚠️ NOT 7497 (paper)
   ```

2. **Risk limits: Reduce size**
   ```yaml
   # configs/risk.yaml
   global:
       max_contracts: 5        # Smaller positions
       max_premium_per_trade: 1000  # Lower budget
       max_daily_loss: 5000    # Stop on loss
   ```

3. **Position sizing: Conservative**
   ```yaml
   # configs/strategies.yaml
   position_sizing_pct: 0.01   # 1% (vs. 2.5% paper)
   ```

4. **Enable Adaptive (if IBKR improves)**
   ```yaml
   # configs/broker.yaml
   adaptive_priority: "Urgent"  # Better fill quality
   ```

5. **Add safeguards:**
   - Run with screen/tmux (won't crash if terminal closes)
   - Enable market data redundancy (backup feeds)
   - Monitor account margin continuously
   - Set hard stop-loss at account level

### Supervised Running (Recommended)

Use `scripts/run_supervised.sh` for automatic restarts on crash:

```bash
#!/bin/bash
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

while true; do
    echo "[$(date)] Starting runner..." >> "$LOG_DIR/supervisor.log"
    python3 -m src.app.runner configs/ >> "$LOG_DIR/runner.log" 2>&1
    
    EXIT_CODE=$?
    echo "[$(date)] Runner exited with code $EXIT_CODE" >> "$LOG_DIR/supervisor.log"
    
    # Wait 5s before restart (avoid restart loop on startup error)
    sleep 5
done
```

```bash
# Run supervised
nohup bash scripts/run_supervised.sh &

# Monitor
tail -f logs/supervisor.log
tail -f logs/runner.log
```

### Docker (Future)

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python3", "-m", "src.app.runner", "configs/"]
```

```bash
docker build -t execution-system .
docker run -it -p 8500:8500 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/configs:/app/configs \
  execution-system
```

---

## Monitoring

### Key Metrics to Watch

**System Health:**
- Tick latency (should be <100ms)
- Database lock contention (rare)
- Reconciliation passes (should be 100%)

**Trading Metrics:**
- Daily PnL (target-dependent)
- Win rate (closes with profit vs. total closes)
- Average duration per trade
- Fill rate (executions / submissions)

**Risk Metrics:**
- Current leverage (should be < max)
- Margin usage (should be < 80%)
- Open position count
- Max single position loss

### Health Check Script

```bash
#!/bin/bash

# Runner running?
if ! pgrep -f "src.app.runner" > /dev/null; then
    echo "❌ Runner not running"
    exit 1
fi

# Dashboard running?
if ! pgrep -f "uvicorn src.dashboard" > /dev/null; then
    echo "❌ Dashboard not running"
    exit 1
fi

# Database accessible?
sqlite3 data/events.db "SELECT COUNT(*) FROM events;" > /dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "❌ Database not accessible"
    exit 1
fi

# Get latest state
STATE=$(curl -s http://localhost:8500/api/state)

# Check system not locked
LOCKED=$(echo "$STATE" | jq -r '.system.locked')
if [ "$LOCKED" = "true" ]; then
    echo "⚠️  System is locked"
    exit 1
fi

# Check positions
POSITIONS=$(echo "$STATE" | jq '.positions | length')
echo "✅ System healthy (positions: $POSITIONS)"
exit 0
```

```bash
# Run periodic health check
while true; do
    bash scripts/health_check.sh
    sleep 60
done
```

### Dashboard Monitoring

Navigate to `http://localhost:8500`:

1. **Live Tab:**
   - Green = system running normally
   - Red icon = system locked (reconciliation failed)
   - Watch unrealized PnL for blown stops

2. **PnL Tab:**
   - Daily breakdown shows performance
   - Per-strategy row shows which strategies are profitable
   - Trends (up/down across days)

3. **Logs Tab:**
   - Filter for "ERROR" to catch problems
   - Watch for "database is locked" spam
   - "CRITICAL" messages indicate manual intervention needed

---

## Incident Response

### Scenario: System Locked (Reconciliation Failure)

**Detection:** Dashboard shows red lock icon, `GET /api/state` returns `system.locked = true`.

**Root Causes:**
- Position discrepancy (broker and internal disagree)
- Orphaned order filled overnight
- Database corruption/crash
- Bug in reconciliation logic

**Resolution Steps:**

1. **Identify mismatch:**
   ```bash
   # From runner logs
   tail -20 data/runner.log | grep -A 5 "Recon"
   
   # Query database
   sqlite3 data/events.db "SELECT * FROM position_attribution WHERE status='OPEN';"
   ```

2. **Understand what happened:**
   - Check event log for unexpected fills
   - Cross-check with IBKR TWS (manual broker position check)
   - Review overnight trades (if applicable)

3. **Reconcile manually:**
   - **Option A:** Restart system (clears positions, re-seeds from broker)
     ```bash
     # Kill runner
     pkill -f "src.app.runner"
     
     # Delete old state
     rm data/events.db
     
     # Restart
     python3 -m src.app.runner configs/
     ```
   
   - **Option B:** Close positions and unlock
     ```bash
     # Close all positions via TWS manually
     # Then unlock system via dashboard command
     curl -X POST http://localhost:8500/api/commands \
       -H "Content-Type: application/json" \
       -d '{"type": "unlock_system", "payload": {}}'
     ```

4. **Verify lock cleared:**
   ```bash
   curl http://localhost:8500/api/state | jq '.system.locked'
   # Should return: false
   ```

### Scenario: Runaway Exit Loop (14 rejections in 14s)

**Detection:** Logs show repeated "Cannot have open orders on both sides" rejections from IBKR.

**Root Cause:** Orphaned entry order blocking exit submission.

**Resolution:**

1. **Find working orders:**
   ```bash
   sqlite3 data/events.db \
     "SELECT order_id, contract, state FROM orders WHERE state='WORKING';"
   ```

2. **Check IBKR TWS:** Does it show orders not in our database?

3. **Force cancel orders:**
   ```bash
   # Via TWS: right-click order → cancel
   # Or via API: craft manual cancel
   ```

4. **Resume trading:**
   ```bash
   # System should auto-retry exit after backoff
   # Or manually submit exit via dashboard
   ```

### Scenario: Dashboard Cached Stale Page

**Detection:** New features/data don't appear even after refresh.

**Root Cause:** Browser serving cached `index.html`.

**Resolution:**
```bash
# Hard refresh (Cmd+Shift+R on Mac, Ctrl+Shift+R on Windows/Linux)
# Or clear browser cache
# Or header already set to no-cache (should be fixed in latest code)
```

### Scenario: "database is locked" Spam

**Detection:** Runner logs show repeated "database is locked" errors.

**Root Cause:**
- Dashboard querying large table while runner writing
- Runner tick taking too long (blocks lock release)
- Multiple processes accessing database

**Resolution:**

1. **Identify heavy query:**
   ```bash
   # Check dashboard logs for slow endpoints
   # Reduce /api/pnl lookback days
   # Reduce /api/history limit
   ```

2. **Increase timeouts:**
   ```yaml
   # configs/dashboard.yaml
   db_timeout: 10.0  # Increase from 5.0
   ```

3. **Batch writes:**
   - Combine multiple position updates into single transaction
   - Reduces lock contention

4. **Restart runner:**
   ```bash
   pkill -f "src.app.runner"
   sleep 2
   python3 -m src.app.runner configs/
   ```

### Scenario: Orders Not Filling

**Detection:** Orders sit at initial price for 60s+ without repricing or filling.

**Root Cause:**
- Reprice task GC'd (task no longer running)
- Quote staleness preventing repricing
- Network latency to IBKR

**Resolution:**

1. **Check reprice running:**
   ```bash
   tail -f data/runner.log | grep -i "repric"
   ```

2. **Check quotes:**
   ```bash
   tail -f data/runner.log | grep "staleness"
   ```

3. **Verify code fix deployed:**
   - Should have strong task references (added 2026-06-12)
   - If still seeing old behavior, runner needs restart with new code

4. **Manual exit:**
   - Orders expire after 60s and are cancelled
   - Can manually submit replacement at better price via dashboard

---

## Maintenance

### Daily Checklist

- [ ] Check dashboard for "Error" or "Critical" logs
- [ ] Verify reconciliation passing (every tick)
- [ ] Monitor daily PnL curve
- [ ] Check no positions stuck in OPENING/CLOSING state
- [ ] Verify database size reasonable (<1GB)

### Weekly Checklist

- [ ] Archive old event logs (`scripts/archive_events.py`)
- [ ] Review incident log for patterns
- [ ] Check IBKR account margin not drifting
- [ ] Verify strategies still profitable (per-day breakdown)
- [ ] Test manual close command (emergency procedure)

### Monthly Checklist

- [ ] Review code changes (what bugs were fixed)
- [ ] Update documentation if behavior changed
- [ ] Optimize database (VACUUM, reindex)
- [ ] Dry-run disaster recovery (database restore from backup)

### Log Rotation

Logs grow quickly (1GB/day in high-activity). Configure rotation:

```yaml
# configs/runner.yaml
logging:
    file: data/runner.log
    rotation: daily              # Rotate daily
    retention_days: 7            # Keep 7 days of logs
    max_size_gb: 5               # Or rotate at 5GB
```

Or manually:

```bash
# Archive old logs
gzip data/runner.log
mv data/runner.log.gz data/runner.log.$(date +%Y%m%d_%H%M%S).gz

# Start fresh
touch data/runner.log
```

### Database Maintenance

```bash
# Check database integrity
sqlite3 data/events.db "PRAGMA integrity_check;"

# Optimize (reclaim space)
sqlite3 data/events.db "VACUUM;"

# Rebuild indexes
sqlite3 data/events.db "REINDEX;"

# Check table sizes
sqlite3 data/events.db ".tables"
sqlite3 data/events.db "SELECT name, sum(pgsize) FROM dbstat GROUP BY name;"
```

### Backup Strategy

```bash
# Daily backup
cp data/events.db data/events.db.$(date +%Y%m%d_%H%M%S).backup

# Cleanup old backups (keep 7 days)
find data/ -name "*.backup" -mtime +7 -delete

# Restore from backup
cp data/events.db.20260612_093000.backup data/events.db
```

---

## Performance Tuning

### Slow Tick Latency

**Target:** <100ms per tick (100 ticks/sec = real-time).

**Profiling:**
```bash
# Add timing to runner
import time
start = time.perf_counter()
# ... tick logic ...
elapsed = (time.perf_counter() - start) * 1000
if elapsed > 100:
    logger.warning(f"Slow tick: {elapsed:.1f}ms")
```

**Common bottlenecks:**

1. **Strategy poll() too slow**
   - Reduce option chain lookups
   - Cache strikes instead of querying each tick
   - Use background thread for heavy computation

2. **Database locks**
   - Use WAL mode (already configured)
   - Batch writes into transactions
   - Reduce query complexity

3. **Quote fetching**
   - Cache quote within 100ms window
   - Skip quote updates if timestamp unchanged

### Database Query Optimization

```bash
# Slow query? Add index
sqlite3 data/events.db "CREATE INDEX idx_events_timestamp ON events(timestamp);"

# Check query plan
sqlite3 data/events.db "EXPLAIN QUERY PLAN SELECT * FROM events WHERE timestamp > ?;"
```

### Memory Leaks

```bash
# Monitor memory usage
ps aux | grep "src.app.runner"

# If growing unbounded, check for:
# - Unclosed database connections
# - Growing collections (orders dict, tasks set)
# - Circular references preventing GC
```

---

## Scaling (Future)

### Multi-Strategy Scaling

Current architecture supports multiple strategies in single process. To scale:

1. **Single runner, many strategies:** Current (4 strategies running)
2. **Multiple runners, different strategies:** Sharded by market
3. **Distributed strategies:** Separate runners per strategy (harder)

### Adding Real-Time Dashboard

Current setup: SQLite writes from runner → HTTP reads from dashboard. For faster updates:

1. Add WebSocket server in runner (publish fills/orders)
2. Dashboard subscribes to runner WebSocket
3. Eliminates SQLite query latency

### Disaster Recovery

If runner crashes:

1. **Positions still open on broker** (broker is authority)
2. **Restart runner:** re-seeds from broker, reconciles
3. **Commands queued during downtime:** executed when runner restarts

For RPO (Recovery Point Objective) < 1s:

- Enable WAL mode (done)
- More frequent event logging (already done)
- Database replication (future: sync to remote DB)

---

## Emergency Procedures

### Kill Switch (Emergency Exit)

Close all positions immediately:

```bash
# Via API
curl -X POST http://localhost:8500/api/commands \
  -H "Content-Type: application/json" \
  -d '{"type": "close_all", "payload": {}}'

# Via TWS (manual)
# Select all orders → right-click → cancel all
```

### Full System Shutdown

```bash
# Stop runner
pkill -f "src.app.runner"

# Stop dashboard
pkill -f "uvicorn"

# Graceful shutdown (waits for cancel ACKs)
# See src/app/runner.py _shutdown() method
```

### Data Backup & Restore

```bash
# Full backup
tar czf backup_$(date +%Y%m%d_%H%M%S).tar.gz data/ configs/

# List backups
ls -lh backup_*.tar.gz

# Restore
tar xzf backup_20260612_100000.tar.gz
```

### Manual Position Sync (Last Resort)

If database corrupted and positions lost:

1. **Get broker positions:**
   ```bash
   # From TWS: Right-click → Account → Account Window
   # Note all open positions (symbol, qty, avg price)
   ```

2. **Manually add to database:**
   ```bash
   sqlite3 data/events.db
   INSERT INTO position_attribution
     (position_id, strategy_id, symbol, expiry, strike, right, side, quantity,
      avg_entry_price, entry_time, status, updated_at)
   VALUES
     ('manual-uuid-1', 'unknown', 'XSP', '20260612', 740, 'CALL', 'SELL', -5,
      2.50, datetime('now'), 'OPEN', datetime('now'));
   ```

3. **Restart runner** → System will reconcile with broker

---

## Performance Benchmarks (Paper Trading)

| Metric | Value | Notes |
|--------|-------|-------|
| Tick latency | 50ms avg | 100 ticks/sec |
| Order fill latency | 2-5s | Depends on IBKR paper |
| Reprice latency | <100ms | In-place modify |
| Dashboard API response | 10-50ms | SQLite reads |
| WebSocket update cadence | 1s | Live state push |

---

## Support & Troubleshooting

### Getting Help

1. **Check logs first:**
   ```bash
   tail -100 data/runner.log | grep -i error
   ```

2. **Query database:**
   ```bash
   sqlite3 data/events.db "SELECT * FROM events ORDER BY timestamp DESC LIMIT 20;"
   ```

3. **Check health:**
   ```bash
   curl http://localhost:8500/api/state | jq '.system'
   ```

4. **Review documentation:**
   - See `docs/INCIDENTS_*.md` for post-mortems
   - See `docs/CONFIGURATION_GUIDE.md` for config issues

### Debug Mode

```bash
# Maximum verbosity
LOG_LEVEL=DEBUG python3 -m src.app.runner configs/

# Tail with grepping
tail -f data/runner.log | grep -E "ERROR|WARN|CRITICAL"
```

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `database is locked` | DB contention | Increase timeout, restart |
| `Cannot connect to TWS` | TWS not running / API disabled | Start TWS, enable API |
| `System is locked` | Reconciliation failed | Check logs, unlock via dashboard |
| `Order not repricing` | Quote stale or task GC'd | Check quote staleness, restart |
| `Position missing after restart` | Not persisted | Check position_attribution table |

