# Paper Trading Checklist

This checklist details the steps required to configure, test, and safely execute the intraday options execution system in **Paper Trading Mode** (both with `MockBrokerClient` and real IBKR Paper trading via TWS/Gateway).

---

## 1. Prerequisites and Setup Verification

- [ ] **Verify IBKR App Configuration**:
  - Open TWS or IB Gateway and log in to a **Paper Trading** account.
  - Verify that the window shows the yellow/orange **Paper Trading** warning header.
  - Confirm API settings:
    - **Enable ActiveX and Socket Clients** is checked.
    - **Read-Only API** is unchecked (required to submit/cancel orders).
    - Socket port is set to `7497` (TWS Paper) or `4002` (Gateway Paper).
    - **Allow connections from localhost only** is checked.

- [ ] **Verify Configuration Files**:
  - In `configs/broker.yaml`, ensure `live_trading.enabled` is `false`:
    ```yaml
    live_trading:
      enabled: false
    ```
  - In `configs/risk.yaml`, verify the global trading mode is set to `paper` or `backtest`:
    ```yaml
    global:
      trading_mode: paper
    ```
  - Confirm that the `Account ID` configured in the system matches your paper account prefix (`DU...`).

- [ ] **Check Environment Variables**:
  - Ensure that the `ALLOW_LIVE_TRADING` environment variable is **NOT** set or is set to a value other than `I_UNDERSTAND_THIS_CAN_LOSE_MONEY` (to prevent accidental live activation).
  - Note: If this environment variable is set to the live value, the system's live trading safety gates will check it. Keep it unset or configured to a dummy value during paper trading for safety.


---

## 2. Connectivity & Permissions Diagnostic

Before running the automated trading system, run the diagnostic scripts to verify connectivity, API capabilities, and market data subscriptions:

- [ ] **Run Connection Check**:
  ```bash
  python3 scripts/check_ibkr_connection.py --port 7497 --client-id 99
  ```
  *Pass criteria:* Successful connection, prints the account list and shows at least one paper account starting with `DU`.

- [ ] **Verify Market Data Feed**:
  ```bash
  python3 scripts/check_market_data.py --symbol SPY --port 7497
  ```
  *Pass criteria:* Prints a non-empty `QuoteSnapshot` with non-zero bid/ask prices and confirms the quote type is `1` (Real-Time).

- [ ] **Verify Option Chain and Greeks**:
  ```bash
  python3 scripts/check_option_chain.py --underlying SPY --port 7497
  ```
  *Pass criteria:* Fetches option chain parameters, lists available expiries and strikes, and retrieves options quotes with valid Greeks (e.g., Delta).

---

## 3. Automated & Integration Testing

Run the full suite of unit and integration tests to ensure code correctness and regression safety:

- [ ] **Run Unit Tests**:
  ```bash
  python3 -m pytest tests/unit/ -v
  ```
  *Pass criteria:* 100% of unit tests pass.

- [ ] **Run Integration Test (Mock Broker)**:
  ```bash
  python3 -m pytest tests/integration/test_mock_execution_flow.py -v
  ```
  *Pass criteria:* The integration test driving the complete signal-to-fill-to-exit flow using the mock broker passes cleanly.

- [ ] **Run Integration Test (Real IBKR Paper Connection)**:
  - Make sure TWS/Gateway is running and logged into Paper trading.
  ```bash
  python3 -m pytest tests/integration/test_ibkr_paper_connection.py -v
  ```
  *Pass criteria:* Connects to the local TWS/Gateway, fetches positions/orders, and exits successfully without throwing errors.

---

## 4. Execution Runner Operations

When running the system using `ExecutionRunner` in paper trading mode:

- [ ] **Initial Reconciliation**:
  - Verify that the runner logs a clean `ReconciliationReport` in the `EventStore` on startup.
  - Verify that if you manually place an order in TWS, the next reconciliation cycle flags a mismatch and locks the system (`system_locked` set to true in overrides).

- [ ] **Manual Control Verification**:
  - Test pausing and resuming strategies:
    ```bash
    python3 src/app/control.py pause-strategy test_strat
    ```
    Verify the runner logs the pause event and stops processing signals for `test_strat`.
  - Test locking the system:
    ```bash
    python3 src/app/control.py lock-system
    ```
    Verify that all new entries are blocked.

- [ ] **Audit Trail Monitoring**:
  - Query the SQLite `EventStore` database to verify that signals, risk decisions, order events, fills, position updates, and manual controls are logged with correct schemas.
