# IBKR Setup and Configuration Guide

This guide details how to install, configure, and connect to Interactive Brokers (IBKR) via Trader Workstation (TWS) or IB Gateway for paper and live trading.

---

## 1. Gateway vs TWS: Choosing an Interface

Interactive Brokers provides two applications to host the API gateway:

| Attribute | IB Gateway (Recommended for Server/Daemon) | Trader Workstation (TWS) (Recommended for Desktop/GUI) |
| :--- | :--- | :--- |
| **GUI Overhead** | Extremely lightweight (command line or basic UI) | Heavy desktop application with rich charting and UI |
| **Auto-Restart** | Can be automated using tools like `IBC` | Difficult to keep open without manual interaction |
| **Memory Footprint** | Low (~200MB) | High (~1GB - 2GB) |
| **API Support** | Identical Socket API capabilities | Identical Socket API capabilities |

*Recommendation*: Use **TWS** for local development, diagnostic testing, and visual order tracking. Use **IB Gateway** on headless server environments.

---

## 2. Port Assignments

By default, IBKR assigns different ports based on the application type and account mode to prevent accidental live execution:

| Client Interface | Live Trading Port | Paper Trading Port (Default) |
| :--- | :--- | :--- |
| **Trader Workstation (TWS)** | `7496` | `7497` |
| **IB Gateway** | `4001` | `4002` |

These ports can be customized in the connection settings.

---

## 3. Configuration Steps

### A. Trader Workstation (TWS) Configuration
1. Open TWS and log in to your **Paper Trading** or **Live Trading** account.
2. Go to **File** > **Global Configuration...** (on macOS: **TWS** > **Global Configuration...**).
3. Navigate to **API** > **Settings** in the left sidebar menu.
4. Apply the following settings:
   - Check **Enable ActiveX and Socket Clients**.
   - Check/Uncheck **Read-Only API** (Note: must be **unchecked** to allow the system to submit and cancel orders; check it only during diagnostic-only runs).
   - Set **Socket port** to `7497` (for Paper) or `7496` (for Live).
   - Ensure **Allow connections from localhost only** is checked to prevent unauthorized network access. If connecting from a local subnet, specify allowed IP addresses under the *Trusted IPs* section.
   - Set **Log Level** to `Detail` or `Error` (recommended: `Error` to keep log directories clean, or `Detail` when debugging connection issues).
5. Click **Apply** and **OK**.

### B. IB Gateway Configuration
1. Open IB Gateway and select the desired API type (usually **IB API**) and mode (**Paper Trading** or **Live Trading**).
2. Log in with your credentials.
3. Go to **Configure** > **Settings** > **API** > **Settings**.
4. Configure settings identical to TWS:
   - Check **Enable ActiveX and Socket Clients**.
   - Set **Socket port** to `4002` (Paper) or `4001` (Live).
   - Disable **Read-Only API** to allow trading.
5. Click **Apply** and **OK**.

---

## 4. Key Differences: Paper vs Live Trading

### Credentials and Account ID
- Paper trading uses a separate username and password prefix.
- Paper trading account IDs start with `DU` (e.g., `DU1234567`). Live account IDs start with a letter followed by numbers (e.g., `U1234567`).
- **Always verify your Account ID in the Client Portal** before initializing connections.

### Connection Boundaries
- The IBKR API connects to either a live backend or a paper/simulated backend based on the credentials of the logged-in session in TWS/Gateway, *not* the port number alone. Connecting to port `7497` on a TWS session logged into a Live account will fail or interact with the Live account if configured to listen on that port.
- **Critical Safety Step**: Ensure TWS/Gateway clearly shows the yellow/orange "Paper Trading" header before launching any automated execution runner.

---

## 5. Client ID Allocations

Multiple API clients can connect to the same TWS/Gateway instance concurrently. They are distinguished by their `client_id` parameter (an integer >= 0).
- **Client ID `0`**: Receives manual executions made inside TWS/Gateway. Avoid using Client ID `0` for automated execution systems to prevent conflicts.
- **Client ID `1` to `999`**: Reserved for the primary runner instance and secondary tools (e.g., diagnostic scripts).
- **Client ID Collisions**: If client ID `1` is already connected, a second attempt to connect with `1` will receive an `API error: Code 326: Client ID already in use` from IBKR. Ensure your production runner and scripts use unique client IDs.

---

## 6. Official IBKR API Resources

- [Interactive Brokers API Home](https://www.interactivebrokers.com/en/trading/ib-api.php)
- [IB Gateway Connection Settings](https://interactivebrokers.github.io/tws-api/initial_setup.html#gsc.tab=0)
- [TWS API Documentation](https://interactivebrokers.github.io/tws-api/)
- [IBC (IB Controller for Headless Setup)](https://github.com/IbcAlpha/IBC)
