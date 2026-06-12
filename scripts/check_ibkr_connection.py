#!/usr/bin/env python3
"""Diagnostic script to check connectivity to IBKR (TWS or IB Gateway).

Places zero orders. Prints connection status, client info, and account IDs.
"""

import argparse
import asyncio
import sys

from ib_async import IB


async def check_connection(host: str, port: int, client_id: int) -> int:
    """Connect to IBKR, retrieve account metadata, and report status."""
    ib = IB()
    print(f"Attempting connection to IBKR at {host}:{port} with client ID {client_id}...")

    try:
        # Wait up to 15 seconds to connect
        await asyncio.wait_for(ib.connectAsync(host, port, clientId=client_id), timeout=15.0)
    except asyncio.TimeoutError:
        print(f"Error: Connection timed out. Is IB Gateway or TWS running at {host}:{port}?", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: Failed to connect to IBKR: {e}", file=sys.stderr)
        return 1

    print("\n=== Connection Successful ===")
    print(f"Connected: {ib.isConnected()}")
    print(f"Server Version: {ib.client.serverVersion()}")

    try:
        # Fetch managed accounts
        accounts = ib.managedAccounts()
        print(f"Managed Accounts: {accounts}")

        for acc in accounts:
            # Paper trading accounts start with 'DU'
            is_paper = acc.startswith("DU")
            mode = "Paper Trading" if is_paper else "LIVE TRADING"
            print(f"  - Account: {acc} ({mode})")

            # Request account summary values to verify data flow
            summary = await ib.accountSummaryAsync(acc)
            if summary:
                net_liq = next((val.value for val in summary if val.tag == "NetLiquidation"), "N/A")
                currency = next((val.currency for val in summary if val.tag == "NetLiquidation"), "")
                print(f"    Net Liquidation: {net_liq} {currency}")

    except Exception as e:
        print(f"Warning: Failed to fetch account summary details: {e}")

    # Explicitly disconnect
    ib.disconnect()
    print("\nDisconnected successfully.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnostic tool: Check connection to IBKR (TWS or IB Gateway)."
    )
    parser.add_argument("--host", default="127.0.0.1", help="API Host IP address")
    parser.add_argument("--port", type=int, default=7497, help="API Socket Port (Default 7497 for TWS Paper)")
    parser.add_argument("--client-id", type=int, default=999, help="API Client ID (Default 999)")

    args = parser.parse_args()

    # Run the async loop
    try:
        code = asyncio.run(check_connection(args.host, args.port, args.client_id))
        sys.exit(code)
    except KeyboardInterrupt:
        print("\nConnection check interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
