#!/usr/bin/env python3
"""Diagnostic script to check the option chain structure for an underlying.

Verifies that IBKR returns option chain parameters (expirations, strikes)
for the given underlying. This confirms the OPRA/options subscription is
delivering chain metadata.

Places zero orders. Does not stream market data.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from ib_async import IB, Index, Stock


# ─── Known symbol presets ──────────────────────────────────────────────
SYMBOL_PRESETS: dict[str, tuple[str, str]] = {
    "XSP": ("IND", "CBOE"),
    "SPX": ("IND", "CBOE"),
    "VIX": ("IND", "CBOE"),
    "NDX": ("IND", "CBOE"),
    "SPY": ("STK", "SMART"),
    "QQQ": ("STK", "SMART"),
    "IWM": ("STK", "SMART"),
}


async def check_option_chain(
    host: str,
    port: int,
    client_id: int,
    underlying_symbol: str,
    sec_type: str,
    exchange: str,
    currency: str,
) -> int:
    """Connect to IBKR, request option chain parameters, and print metadata."""
    ib = IB()
    print(f"Connecting to IBKR at {host}:{port} (client {client_id})...")
    try:
        await asyncio.wait_for(ib.connectAsync(host, port, clientId=client_id), timeout=15.0)
    except Exception as e:
        print(f"Error: Failed to connect to IBKR: {e}", file=sys.stderr)
        return 1

    # ── Qualify underlying ──────────────────────────────────────────
    sec_upper = sec_type.upper()
    print(f"\nCreating underlying: {underlying_symbol} (secType={sec_upper}, exchange={exchange})...")

    if sec_upper in ("STK", "STOCK"):
        underlying = Stock(underlying_symbol, exchange, currency)
    elif sec_upper in ("IND", "INDEX"):
        underlying = Index(underlying_symbol, exchange, currency)
    else:
        print(f"Error: Security type '{sec_type}' not supported. Use IND or STK.", file=sys.stderr)
        ib.disconnect()
        return 1

    print("Qualifying underlying contract...")
    try:
        qualified = await ib.qualifyContractsAsync(underlying)
    except Exception as e:
        print(f"Error qualifying contract: {e}", file=sys.stderr)
        ib.disconnect()
        return 1

    if not qualified or qualified[0].conId == 0:
        print(f"✗ Failed to qualify {underlying_symbol} as {sec_upper} on {exchange}.")
        if sec_upper in ("STK", "STOCK"):
            print(f"  Hint: {underlying_symbol} may be an index. Try --sec-type IND --exchange CBOE")
        ib.disconnect()
        return 1

    target_underlying = qualified[0]
    print(f"✓ Qualified: conId={target_underlying.conId}, "
          f"exchange={target_underlying.exchange}, "
          f"secType={target_underlying.secType}")

    # ── Fetch option chain parameters ───────────────────────────────
    print(f"\nFetching option chain parameters (reqSecDefOptParams)...")
    try:
        chains = await ib.reqSecDefOptParamsAsync(
            underlying_symbol,
            "",
            target_underlying.secType,
            target_underlying.conId,
        )
    except Exception as e:
        print(f"Error: Failed to retrieve option chain parameters: {e}", file=sys.stderr)
        ib.disconnect()
        return 1

    if not chains:
        print(f"\n✗ No option chains returned for {underlying_symbol}.")
        print(f"  Possible causes:")
        print(f"  1. OPRA (US Options) subscription not active in IBKR Account Management")
        print(f"  2. The symbol does not have listed options")
        print(f"  3. TWS/Gateway needs restart after subscription change")
        ib.disconnect()
        return 1

    today_str = datetime.now(UTC).strftime("%Y%m%d")

    print(f"\n✓ Found {len(chains)} option chain definition(s):\n")
    for i, chain in enumerate(chains):
        expiries = sorted(list(chain.expirations))
        strikes = sorted(list(chain.strikes))

        # Count future expiries
        future_expiries = [e for e in expiries if e >= today_str]

        print(f"  Chain #{i+1}:")
        print(f"    Exchange:      {chain.exchange}")
        print(f"    Trading Class: {chain.tradingClass}")
        print(f"    Multiplier:    {chain.multiplier}")
        print(f"    Expirations:   {len(expiries)} total, {len(future_expiries)} future")
        print(f"    Strikes:       {len(strikes)}")

        # Show nearest expiries
        if future_expiries:
            show_expiries = future_expiries[:7]
            formatted = [f"{e[:4]}-{e[4:6]}-{e[6:]}" for e in show_expiries]
            print(f"    Next expiries: {', '.join(formatted)}")
            if len(future_expiries) > 7:
                print(f"                   ... and {len(future_expiries) - 7} more")

        # Show strike range
        if strikes:
            print(f"    Strike range:  {strikes[0]} — {strikes[-1]}")

            # Show strikes near a round number for context
            # e.g., if XSP is ~560, show strikes around 555-565
            mid_idx = len(strikes) // 2
            sample_start = max(0, mid_idx - 5)
            sample_end = min(len(strikes), mid_idx + 5)
            sample = [str(s) for s in strikes[sample_start:sample_end]]
            print(f"    Mid strikes:   {', '.join(sample)}")

        # Estimate total contracts
        total_estimated = len(future_expiries) * len(strikes) * 2  # Call + Put
        print(f"    Est. active contracts: ~{total_estimated:,}")
        print(f"    ⚠ IBKR limits ~100 simultaneous streaming tickers")
        print()

    # ── Summary ─────────────────────────────────────────────────────
    total_chains = len(chains)
    smart_chains = [c for c in chains if c.exchange == "SMART"]
    cboe_chains = [c for c in chains if c.exchange == "CBOE"]

    print(f"{'='*50}")
    print(f"  SUMMARY: {underlying_symbol} Option Chain")
    print(f"{'='*50}")
    print(f"  Total chain definitions: {total_chains}")
    if smart_chains:
        print(f"  SMART routing available: ✓ ({len(smart_chains)} chain(s))")
    if cboe_chains:
        print(f"  CBOE direct available:   ✓ ({len(cboe_chains)} chain(s))")
    print(f"  OPRA subscription:       ✓ CONFIRMED (chains returned)")

    ib.disconnect()
    print(f"\nDisconnected.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnostic: Check option chain metadata for an underlying. "
                    "Confirms OPRA subscription is active."
    )
    parser.add_argument("--host", default="127.0.0.1", help="API Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7497, help="API Port (default: 7497)")
    parser.add_argument("--client-id", type=int, default=999, help="API Client ID (default: 999)")
    parser.add_argument("--underlying", default="XSP", help="Underlying symbol (default: XSP)")
    parser.add_argument(
        "--sec-type", default=None,
        help="Security type override (IND, STK). Auto-detected from presets if omitted."
    )
    parser.add_argument(
        "--exchange", default=None,
        help="Exchange override (CBOE, SMART). Auto-detected from presets if omitted."
    )
    parser.add_argument("--currency", default="USD", help="Currency (default: USD)")

    args = parser.parse_args()

    # Auto-detect from presets
    symbol = args.underlying.upper()
    if args.sec_type is None or args.exchange is None:
        if symbol in SYMBOL_PRESETS:
            preset_sec, preset_exch = SYMBOL_PRESETS[symbol]
            sec_type = args.sec_type or preset_sec
            exchange = args.exchange or preset_exch
            print(f"[Auto-detected] {symbol}: secType={sec_type}, exchange={exchange}")
        else:
            sec_type = args.sec_type or "STK"
            exchange = args.exchange or "SMART"
            print(f"[No preset for {symbol}] Using: secType={sec_type}, exchange={exchange}")
    else:
        sec_type = args.sec_type
        exchange = args.exchange

    try:
        code = asyncio.run(
            check_option_chain(
                args.host,
                args.port,
                args.client_id,
                symbol,
                sec_type,
                exchange,
                args.currency,
            )
        )
        sys.exit(code)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
