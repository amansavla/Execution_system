# Development Guide

Guidelines for developing, testing, and debugging the system.

---

## Setup

### Prerequisites

```bash
# Python 3.9+ (tested on 3.10+)
python3 --version

# Virtual environment
python3 -m venv venv
source venv/bin/activate

# Dependencies
pip install -r requirements.txt

# For development
pip install -e .  # Install in editable mode
```

### Project Structure

```
src/
├── app/                    # Main runner + control flow
│   ├── runner.py          # Orchestration loop
│   ├── control.py         # Manual control
│   └── status.py          # Status checks
├── broker/                # IBKR connection
│   ├── ibkr_broker.py     # Main broker class
│   ├── interface.py       # Broker protocol
│   └── mock_broker.py     # Testing mock
├── execution/             # Order management
│   └── order_manager.py   # Order lifecycle tracking
├── portfolio/             # Position management
│   ├── position_manager.py
│   ├── exit_manager.py
│   └── reconciliation.py
├── risk/                  # Risk checking
│   └── risk_engine.py     # Position size limits
├── storage/               # Persistence
│   ├── event_log.py       # Event sourcing
│   ├── position_store.py  # Position attribution
│   └── runtime_state.py   # Snapshot storage
├── strategies/            # Entry signal generators
│   ├── xsp_short_straddle.py
│   ├── xsp_breakout.py
│   └── dummy_test.py
└── dashboard/             # HTTP API + UI
    ├── app.py             # FastAPI server
    └── static/            # Frontend

tests/
├── unit/                  # Pure unit tests (no IO)
├── integration/           # Full system tests (with mock broker)
└── fixtures/              # Test data and mocks

configs/
├── broker.yaml            # IBKR settings
├── risk.yaml              # Risk limits
├── strategies.yaml        # Strategy config
└── ...
```

---

## Testing

### Running Tests

```bash
# All tests
pytest -v

# Just unit tests
pytest tests/unit/ -v

# Just integration tests
pytest tests/integration/ -v

# Specific test file
pytest tests/unit/test_risk_engine.py -v

# Specific test
pytest tests/unit/test_risk_engine.py::test_premium_clamping -v

# With coverage
pytest --cov=src --cov-report=html
```

### Test Organization

**Unit Tests** (`tests/unit/`)
- No external dependencies (mock everything)
- Fast (<1ms each)
- Pure function testing (input → output)
- Examples:
  - `test_risk_engine.py` — Risk checking logic
  - `test_order_manager.py` — Order state machine
  - `test_models.py` — Data model validation

**Integration Tests** (`tests/integration/`)
- Use mock broker (not real IBKR)
- Test full workflows (entry → fill → exit)
- Slower (~100ms-1s each)
- Examples:
  - `test_mock_execution_flow.py` — Submit → fill → close
  - `test_multileg_coordination.py` — Straddle entry/exit
  - `test_resilience.py` — Reconnect, timeout, lock scenarios

### Writing Tests

**Example Unit Test:**

```python
# tests/unit/test_risk_engine.py
def test_premium_clamping():
    """Max premium per trade limits large orders."""
    engine = RiskEngine(
        max_contracts=10,
        max_premium_per_trade=2000.0  # $2000 max
    )
    
    signal = Signal(
        qty=10,
        limit_price=3.00,  # $300 per contract
        # Unclamped: 10 * $300 = $3000 > $2000 budget
    )
    
    allowed_qty = engine.compute_allowed_qty(signal)
    
    # Expected: 2000 / 300 = 6.66 → 6 contracts
    assert allowed_qty == 6
```

**Example Integration Test:**

```python
# tests/integration/test_mock_execution_flow.py
@pytest.mark.asyncio
async def test_entry_and_exit():
    """Full lifecycle: entry → fill → exit → close."""
    broker = MockBroker()
    manager = OrderManager(broker)
    portfolio = PortfolioManager()
    
    # 1. Submit entry order
    order_id = broker.submit_order(
        contract=XSP_740_CALL,
        side=SELL,
        qty=5,
        limit=2.50
    )
    
    # Order should be WORKING
    assert manager.order_state(order_id) == OrderState.WORKING
    
    # 2. Simulate fill
    broker.fill_order(order_id, price=2.49, qty=5)
    await asyncio.sleep(0.1)  # Let async handlers run
    
    # Position should exist
    position = portfolio.find_position(XSP_740_CALL)
    assert position.quantity == -5
    assert position.entry_price == 2.49
    
    # 3. Submit exit
    exit_order_id = broker.submit_order(
        contract=XSP_740_CALL,
        side=BUY,
        qty=5,
        limit=2.40
    )
    
    # 4. Fill exit
    broker.fill_order(exit_order_id, price=2.40, qty=5)
    await asyncio.sleep(0.1)
    
    # Position should be closed
    assert position.status == PositionStatus.CLOSED
    assert position.realized_pnl == 50.0  # (2.49 - 2.40) * 100 * 5
```

### Test Fixtures

Common fixtures in `tests/conftest.py`:

```python
@pytest.fixture
def mock_broker():
    """Mock IBKR broker for testing."""
    return MockBroker()

@pytest.fixture
def risk_engine():
    """Configured risk engine."""
    return RiskEngine(
        max_contracts=10,
        max_premium_per_trade=2000.0
    )

@pytest.fixture
def runner(mock_broker):
    """Fully configured runner with mock broker."""
    return Runner(broker=mock_broker, config=...)
```

---

## Development Workflow

### Running the System Locally

**Terminal 1: Runner**
```bash
# Start runner (connects to TWS at port 7497 for paper trading)
python3 -m src.app.runner configs/

# Or with debug logging
LOG_LEVEL=DEBUG python3 -m src.app.runner configs/
```

**Terminal 2: Dashboard**
```bash
# Start dashboard server
python3 -m uvicorn src.dashboard.app:app --port 8500 --reload

# Navigate to http://localhost:8500
```

**Terminal 3: Monitoring**
```bash
# Watch runner logs in real-time
tail -f data/runner.log | grep -E "FILL|EXIT|ERROR"

# Or query database for recent events
watch -n 1 'sqlite3 data/events.db "SELECT COUNT(*) FROM events; SELECT event_type, COUNT(*) FROM events GROUP BY event_type;"'
```

### Code Style

**Follow PEP 8:**
```bash
# Format code
black src/ tests/

# Check style
flake8 src/ tests/ --max-line-length=120

# Type checking
mypy src/ --ignore-missing-imports
```

**Naming Conventions:**
- Classes: `PascalCase` (e.g., `OrderManager`)
- Functions: `snake_case` (e.g., `_submit_exit`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `MAX_QUOTE_STALENESS`)
- Private methods: `_prefixed` (e.g., `_background_tasks`)

**Comments:**
- No unnecessary comments (good variable names are better)
- Comments explain WHY, not WHAT
- Example:
```python
# ✓ Good
# Retry 3 times on DB lock; WAL mode keeps readers non-blocking
for attempt in range(3):
    try:
        conn.execute(...)
        break
    except sqlite3.OperationalError as e:
        if "locked" in str(e) and attempt < 2:
            time.sleep(0.25 * (attempt + 1))

# ✗ Bad
# Try to execute
for attempt in range(3):
    try:
        conn.execute(...)  # Execute command
```

### Debugging

**Add logging:**
```python
import logging

logger = logging.getLogger(__name__)

def submit_exit(position):
    logger.debug(f"Submitting exit for {position.contract}")
    try:
        order_id = broker.submit_order(...)
        logger.info(f"Exit order {order_id} submitted")
    except Exception as e:
        logger.error(f"Exit submission failed: {e}", exc_info=True)
```

**Check database directly:**
```bash
sqlite3 data/events.db

# Recent events
SELECT timestamp, event_type, payload FROM events 
ORDER BY timestamp DESC LIMIT 10;

# Positions
SELECT strategy_id, symbol, strike, side, quantity, status
FROM position_attribution
WHERE status = 'OPEN';

# Commands pending
SELECT command_id, type, payload, status
FROM commands WHERE status = 'pending';
```

**Use Python debugger:**
```python
import pdb; pdb.set_trace()

# Or in newer Python:
breakpoint()  # Set breakpoint
# 'n' = next line, 's' = step into, 'c' = continue
```

---

## Common Development Tasks

### Add a New Strategy

1. Create file `src/strategies/my_strategy.py`

```python
from src.core.models import Signal, Leg, Side
from src.strategies import StrategyProvider

class MyStrategy(StrategyProvider):
    def __init__(self, config):
        self.config = config
    
    def poll(self, state):
        """Generate entry signal if conditions met."""
        # Check entry window
        if not self._in_entry_window():
            return None
        
        # Check if already in position
        if self._position_exists(state):
            return None
        
        # Get signal
        signal = self._compute_signal(state)
        return signal
    
    def create_orders(self, qty, limit_price):
        """Create individual leg orders."""
        return [
            Order(contract=call, side=SELL, qty=qty, limit=limit_price),
            Order(contract=put, side=SELL, qty=qty, limit=limit_price),
        ]
```

2. Register in `src/strategies/__init__.py`

```python
from src.strategies.my_strategy import MyStrategy

__all__ = [
    'XSPShortStraddle',
    'XSPBreakout',
    'MyStrategy',  # Add here
]
```

3. Add config in `configs/strategies.yaml`

```yaml
my_strategy:
    enabled: true
    entry.entry_time: "10:00"
    entry.entry_time_end: "12:00"
    exit.stop_loss_pct: 50.0
    exit.time_exit_utc: "16:00"
    position_sizing_pct: 0.02
```

4. Write tests in `tests/unit/test_my_strategy.py`

```python
def test_entry_signal():
    strategy = MyStrategy(config={...})
    state = PortfolioState(open_positions=[])
    
    signal = strategy.poll(state)
    
    assert signal is not None
    assert signal.qty == 5
```

### Add a New Risk Check

1. Edit `src/risk/risk_engine.py`

```python
def compute_allowed_qty(self, signal):
    qty = signal.qty
    
    # ... existing checks ...
    
    # New check: max leverage
    if self._risk.global_.max_leverage:
        margin_used = self._compute_margin_used()
        available_leverage = self.account_buying_power / self.account_value
        if available_leverage < self._risk.global_.max_leverage:
            qty = 0  # No room for more leverage
    
    return qty
```

2. Add config in `configs/risk.yaml`

```yaml
global:
    max_leverage: 5.0  # Max 5x leverage
```

3. Write test in `tests/unit/test_risk_engine.py`

```python
def test_leverage_limit():
    engine = RiskEngine(max_leverage=5.0)
    engine._account_value = 10000
    engine._margin_used = 40000  # 4x leverage
    
    signal = Signal(qty=5, limit_price=5.00)
    
    # 5 more contracts would push to 5x, which is OK
    assert engine.compute_allowed_qty(signal) == 5
```

### Fix a Bug

**Step 1: Write a failing test**
```python
def test_stop_loss_doesnt_trigger_on_stale_quote():
    """Regression: stop-loss fired on quote > 5s old."""
    position = Position(...)
    quote = Quote(bid=2.50, ask=2.52, staleness=6.5)  # Stale!
    
    exit_triggered = exit_manager.check_stop_loss(position, quote)
    
    assert not exit_triggered  # Should not exit on stale quote
```

**Step 2: Make test pass**
```python
def check_stop_loss(self, position, quote):
    # Only check if quote is fresh
    if quote.staleness > MAX_QUOTE_STALENESS:
        return False
    
    # ... rest of stop-loss logic
```

**Step 3: Run tests**
```bash
pytest tests/ -v
```

**Step 4: Commit**
```bash
git add -A
git commit -m "Fix: don't trigger SL on stale quotes (>5s)"
```

---

## Benchmarking & Profiling

### Measure Tick Latency

```python
# In runner._tick():
import time

start = time.perf_counter()

# ... tick logic ...

elapsed_ms = (time.perf_counter() - start) * 1000
if elapsed_ms > 100:  # Warn if tick takes >100ms
    logger.warning(f"Slow tick: {elapsed_ms:.1f}ms")
```

### Profile with cProfile

```bash
# Profile runner for 10 seconds
python3 -m cProfile -o runner.prof -m src.app.runner configs/ &
sleep 10
kill %1

# Analyze results
python3 -m pstats runner.prof
# At prompt: sort cumtime, top 20
```

### Memory Profiling

```bash
pip install memory_profiler

# Profile specific function
@profile
def _tick(self):
    # ... tick logic ...
```

```bash
python3 -m memory_profiler runner.py
```

---

## Continuous Integration (Future)

Recommended GitHub Actions workflow:

```yaml
# .github/workflows/tests.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.10'
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v --cov=src
      - run: black --check src/
      - run: flake8 src/ --max-line-length=120
```

---

## Troubleshooting Development Issues

### "database is locked" during tests

```python
# Set longer timeout
conn = sqlite3.connect(':memory:', timeout=10.0)

# Or use SQLite in-memory for tests
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
```

### Async tests hanging

```python
# Add timeout
@pytest.mark.timeout(5)
@pytest.mark.asyncio
async def test_something():
    ...
```

### Mock broker not filling orders

```python
# Manually trigger fill
broker.fill_order(order_id, price=2.50, qty=5)
await asyncio.sleep(0.1)  # Let handlers run
```

### Dashboard not picking up code changes

```bash
# Restart with reload enabled
python3 -m uvicorn src.dashboard.app:app --reload

# Or manually restart on code changes
```

---

## Performance Optimization Checklist

- [ ] Profile hot paths with cProfile
- [ ] Minimize database queries (batch when possible)
- [ ] Use indexes on large queries (position_attribution.status, events.timestamp)
- [ ] Cache expensive computations (quote staleness checks)
- [ ] Use WAL mode for SQLite (non-blocking readers)
- [ ] Run DB touches on threadpool (not event loop)
- [ ] Hold strong references to asyncio tasks (prevents GC)
- [ ] Limit event log size (prune old events)

---

## Release Checklist

Before pushing to main:

- [ ] All tests pass (`pytest tests/ -v`)
- [ ] Code formatted (`black src/`)
- [ ] Style clean (`flake8 src/`)
- [ ] Types pass (`mypy src/`)
- [ ] Documentation updated
- [ ] Changelog entry added
- [ ] No hardcoded credentials or test data left
- [ ] Live tested if code affects order execution
