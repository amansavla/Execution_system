# Live Trading Checklist

This checklist documents every mandatory manual verification step that **MUST** be performed and marked as passed before enabling live trading in production.

---

## 1. Paper Mode Stability Confirmation

Before attempting to activate live mode, the system must demonstrate sustained, exception-free behavior in paper trading.

- [ ] **Stable Run Duration**: Run the system continuously in paper trading mode for at least **5 consecutive trading days** under normal market conditions.
- [ ] **No Unhandled Exceptions**: Audit system log files (`EventStore` database and application logs) to verify that zero unhandled exceptions or crashes occurred.
- [ ] **Memory and Resource Check**: Verify that memory usage and CPU footprint remained stable, showing no signs of memory leaks over the multi-day run.

---

## 2. Manual Flatten Test

Verify that manual position flattening is responsive and functions as a guaranteed exit mechanism.

- [ ] **Single Position Flatten**:
  - Open a paper position.
  - Dispatch a manual flatten command via the dashboard or CLI:
    ```bash
    python3 src/app/control.py flatten-position --position-id <id>
    ```
  - Verify that:
    1. Active limit orders for that contract are cancelled.
    2. An emergency market/limit exit order is immediately submitted.
    3. The position successfully transitions to `CLOSED` state.
- [ ] **Global Flatten All**:
  - Open multiple paper positions across different underlyings.
  - Dispatch the global flatten command:
    ```bash
    python3 src/app/control.py flatten-all
    ```
  - Verify that all open positions are immediately exited, and all pending orders are cancelled.

---

## 3. Kill Switch Test

Verify that risk limits and connectivity drops trigger the safety shutdown correctly.

- [ ] **Max Daily Loss Limit Trigger**:
  - Temporarily configure `configs/risk.yaml` with an extremely low daily loss limit.
  - Execute a mock trade that registers a loss exceeding the limit.
  - Verify that:
    1. The `RiskEngine` transitions the system into `locked` mode.
    2. Any new signal is rejected with a clear daily loss limit breach reason.
    3. If configured to auto-flatten, all active positions are closed.
- [ ] **Broker Disconnect Simulation**:
  - Connect to the paper broker and start running the system.
  - Simulate a network disconnect (e.g., pause the IB Gateway process or pull network).
  - Verify that:
    1. The `ReconnectionConfig` handles the disconnect and attempts reconnection.
    2. During the disconnect, `QuoteCache` invalidates quotes and marks them as stale.
    3. No new signals are accepted while disconnected.

---

## 4. Reconciliation Test

Verify that state discrepancies between the internal book and the broker are caught and immediately lock down the system.

- [ ] **Position Quantity Mismatch**:
  - With the runner running in paper mode, open TWS/Gateway.
  - Manually trade a contract in TWS (bypassing the runner) to create a position that the runner does not know about.
  - Wait for the next reconciliation cycle (or restart the runner).
  - Verify that:
    1. The reconciliation mismatch is detected.
    2. A `reconciliation_event` is logged to the `EventStore`.
    3. The runner transition to `locked` or `reduce-only` mode.
    4. New entry order submissions are blocked.
- [ ] **Unknown Order Discrepancy**:
  - Manually submit a limit order in TWS that does not exist in the runner's internal `OrderManager`.
  - Verify that the reconciliation engine flags this unknown broker order, logs the discrepancy, and locks the system.

---

## 5. Dashboard Controls Test

Verify that dangerous actions on the Streamlit dashboard require explicit confirmation to avoid accidental activation.

- [ ] **Flatten Button Gate**:
  - Open the positions tab on the Streamlit dashboard.
  - Click the **Flatten** button for a position.
  - Verify that it does not immediately dispatch an order, but instead presents a clear confirmation step (e.g. dynamic confirmation button).
  - Verify that the action is only dispatched after the confirmation is clicked, and the confirmation state resets correctly afterward.
- [ ] **Lock System Gate**:
  - Click the **Lock System** button on the risk control dashboard panel.
  - Verify that an explicit confirmation dialog or secondary confirm button is presented.
  - Confirm the lock, and verify the system overrides update to set `system_locked: true` in `configs/overrides.yaml`.

---

## 6. Live Mode Safety Gates Checklist

Only check these off when actually ready to deploy.

- [ ] Ensure `live_trading.enabled` is set to `true` in `configs/broker.yaml`.
- [ ] Ensure environment variable `ALLOW_LIVE_TRADING` is set to `I_UNDERSTAND_THIS_CAN_LOSE_MONEY`.
- [ ] Ensure the exact live account ID matches the allowlist in `configs/broker.yaml`.
- [ ] Confirm no paper trading account IDs (`DU...` or `DF...`) are present in the allowlist.
