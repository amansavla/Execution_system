# IBKR Market Data Requirements

To run this options execution system, you must subscribe to specific real-time market data packages through Interactive Brokers. Without these subscriptions, the system will receive delayed or blank quotes, triggering safety gates and rejecting all trades.

---

## 1. Required Subscriptions

All subscriptions must be activated in the **IBKR Client Portal** under **User Settings** > **Market Data Subscriptions**.

| Package Name | Exchange / Provider | Purpose | Monthly Cost (Est.) |
| :--- | :--- | :--- | :--- |
| **US Securities Snapshot and Futures Value Bundle** | NYSE/AMEX/NASDAQ/ARCA | Real-time underlying equity bid, ask, and last price | Free if minimum commissions met |
| **US Equity Options / OPRA** | OPRA (Options Price Reporting Authority) | Real-time bid, ask, and Greeks for options contracts | ~$1.50/mo (waived if commission minimum met) |
| **NASDAQ TotalView-OpenView** (Optional) | NASDAQ | Extra book depth for NASDAQ equities | ~$15.00/mo |
| **NYSE OpenBook** (Optional) | NYSE | Extra book depth for NYSE equities | ~$15.00/mo |

> [!IMPORTANT]
> **Delayed Data Warning**: If you do not purchase the above subscriptions, IBKR API returns delayed quotes (indicated by quote types other than `1` / real-time, or returning `0.0` or `None` for bid/ask). Delayed data is rejected by the `RiskEngine` (freshness check).

---

## 2. Greeks Generation Requirements

In IBKR, Greeks (Delta, Gamma, Vega, Theta, Implied Volatility) are computed by the exchange or local TWS model and returned via market data.
- **Underlying Quote Freshness**: For TWS to calculate Greeks dynamically, the underlying stock/ETF must also have a live real-time feed active in the same session.
- **Model Parameters**: Make sure your local TWS settings (**Global Configuration** > **Volatility**) are configured correctly (e.g., matching target interest rates and model type) if you rely on TWS-calculated Greeks.
- **OPRA Greeks**: The `OPRA` subscription is the minimum required to receive greeks values on option snapshots via the API.
- **Greeks Availability**: Greeks are typically populated only when there is active market activity (i.e. during market hours). During pre-market/after-market, Greeks might be `None`.

---

## 3. Market Data Line Limits (Ticker Limits)

Interactive Brokers enforces a limit on the number of simultaneous active market data subscriptions (tickers) you can maintain:
- **Base Limit**: A standard retail account receives a baseline of **100 tickers** (simultaneous market data lines).
- **Increases**: Your ticker limit increases dynamically based on commissions spent or account equity. (See the official [IBKR Market Data Ticker Limits](https://www.interactivebrokers.com/lib/cstools/faq/#/content/faq%2F58682855) FAQ).
- **Why full option chain streaming is avoided**: A single underlying index/stock can have thousands of active options contracts. Subscribing to stream the entire chain will immediately breach your ticker limit, causing all subsequent market data requests to fail with `Max ticker limit reached`.
- **System Pattern**: The system uses the `OptionContractSelector` to filter strikes and expiries before requesting quotes. It only requests market data for the targeted list of candidate contracts (e.g. the 2-4 closest strikes to the money) and cancels the subscriptions when no longer needed.

---

## 4. System Behavior on Market Data Issues

Our system operates under a strict **fail-closed** paradigm (per AGENTS.md rule 7) when encountering data quality anomalies:

| Issue | Detection | System Action | Rationale |
| :--- | :--- | :--- | :--- |
| **Delayed Data** | Quote type is not `1` (real-time) or timestamp is old. | Rejected by `RiskEngine` / `ExitManager` | Delayed data leads to execution at incorrect prices. |
| **Stale Data** | `timestamp` age exceeds `quote_max_age_seconds` in config. | Rejected by `RiskEngine` / `ExitManager` | Limits risk during rapid market movements or connection drops. |
| **Missing Quotes** | `bid` or `ask` is `None` (empty) in QuoteSnapshot. | Rejected by `RiskEngine` / `ExitManager` | Prevents submitting orders without validating spread width and price boundaries. |
| **Permission Denied** | API error message or code indicating no subscription. | Rejected / Lock system | Operator must resolve billing or permission status before system resumes. |

---

## 5. Official Resources and Verification

- To verify active subscriptions, log in to your **IBKR Client Portal** and view your market data configuration.
- Learn more about IBKR pricing and packages: [Interactive Brokers Market Data Pricing](https://www.interactivebrokers.com/en/pricing/market-data-subscriptions.php)
- Official documentation on option market data: [TWS API Option Chains](https://interactivebrokers.github.io/tws-api/options.html)
