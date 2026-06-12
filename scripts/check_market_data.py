#!/usr/bin/env python3
"""Diagnostic script to check market data feeds for underlyings and options.

Verifies:
  - Underlying index/stock quote feed (CBOE for XSP/SPX)
  - Near-ATM option quote + greeks (OPRA feed)
  - Subscription status for both CBOE and OPRA

Places zero orders. Read-only diagnostic.
"""

import argparse
import asyncio
import math
import sys
from datetime import UTC, datetime, timedelta

from ib_async import IB, Contract, Index, Option, Stock


# ─── Known symbol presets ──────────────────────────────────────────────
# Maps symbol -> (secType, exchange) for common underlyings so the user
# doesn't have to remember "XSP is IND on CBOE" every time.
SYMBOL_PRESETS: dict[str, tuple[str, str]] = {
    "XSP": ("IND", "CBOE"),
    "SPX": ("IND", "CBOE"),
    "VIX": ("IND", "CBOE"),
    "NDX": ("IND", "CBOE"),
    "SPY": ("STK", "SMART"),
    "QQQ": ("STK", "SMART"),
    "IWM": ("STK", "SMART"),
}


def _is_valid_price(val: object) -> bool:
    """Return True if val is a usable numeric price (not None, not nan, not -1)."""
    if val is None:
        return False
    try:
        return not math.isnan(float(val)) and float(val) > 0
    except (TypeError, ValueError):
        return False


def _fmt_price(val: object) -> str:
    """Format a price for display, handling nan/None."""
    if _is_valid_price(val):
        return f"{float(val):>12.2f}"
    return "         N/A"


def _build_underlying_contract(symbol: str, sec_type: str, exchange: str, currency: str) -> Contract:
    """Build the correct ib_async contract object for the underlying."""
    sec_type_upper = sec_type.upper()
    if sec_type_upper in ("IND", "INDEX"):
        return Index(symbol, exchange, currency)
    elif sec_type_upper in ("STK", "STOCK"):
        return Stock(symbol, exchange, currency)
    else:
        return Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)


async def check_market_data(
    host: str,
    port: int,
    client_id: int,
    symbol: str,
    sec_type: str,
    exchange: str,
    currency: str,
    check_options: bool,
) -> int:
    """Connect to IBKR and check real-time market data for underlying + options."""
    ib = IB()
    print(f"Connecting to IBKR at {host}:{port} (client {client_id})...")
    try:
        await asyncio.wait_for(ib.connectAsync(host, port, clientId=client_id), timeout=15.0)
    except Exception as e:
        print(f"Error: Failed to connect to IBKR: {e}", file=sys.stderr)
        return 1

    # ── Step 1: Qualify underlying ──────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  UNDERLYING: {symbol}  secType={sec_type}  exchange={exchange}")
    print(f"{'='*60}")

    contract = _build_underlying_contract(symbol, sec_type, exchange, currency)
    print(f"Qualifying {symbol} ({sec_type}) on {exchange}...")

    try:
        qualified = await ib.qualifyContractsAsync(contract)
    except Exception as e:
        print(f"Error qualifying contract: {e}", file=sys.stderr)
        ib.disconnect()
        return 1

    # qualifyContractsAsync returns a list; entries can be the contract or None/unchanged
    if not qualified or qualified[0].conId == 0:
        print(f"\n✗ FAILED to qualify {symbol} as {sec_type} on {exchange}.")
        print(f"  This means IBKR does not recognize this contract definition.")
        if sec_type.upper() in ("STK", "STOCK"):
            print(f"  Hint: {symbol} may be an index (IND), not a stock. Try --sec-type IND --exchange CBOE")
        ib.disconnect()
        return 1

    underlying = qualified[0]
    print(f"✓ Qualified: conId={underlying.conId}, exchange={underlying.exchange}, "
          f"primaryExchange={getattr(underlying, 'primaryExchange', 'N/A')}")

    # ── Step 2: Register error handler ────────────────────────────────
    # Register BEFORE any data requests
    errors: list[tuple[int, str]] = []

    def on_error(*args):
        # ib_async errorEvent can emit (reqId, errorCode, errorString, contract)
        # or (reqId, errorCode, errorString) depending on version
        if len(args) >= 3:
            errorCode, errorString = args[1], args[2]
        elif len(args) == 1 and hasattr(args[0], 'errorCode'):
            errorCode = args[0].errorCode
            errorString = getattr(args[0], 'errorString', str(args[0]))
        else:
            print(f"  [DEBUG] Unexpected error event args: {args}")
            return
        errors.append((int(errorCode), str(errorString)))
        print(f"  [IBKR msg {errorCode}] {errorString}")

    ib.errorEvent += on_error

    # ── Step 3: Request underlying market data ──────────────────────
    # Try real-time first (Type 1)
    print(f"\nRequesting real-time market data (Type 1) for {symbol}...")
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(underlying, "", False, False)

    print("Waiting for market data (5 seconds)...")
    for i in range(5):
        await asyncio.sleep(1.0)
        if _is_valid_price(ticker.bid) or _is_valid_price(ticker.last):
            break

    # If no real-time, try delayed (Type 3)
    has_any = _is_valid_price(ticker.bid) or _is_valid_price(ticker.ask) or _is_valid_price(ticker.last)
    if not has_any:
        print("\nNo real-time data. Trying delayed data (Type 3)...")
        ib.cancelMktData(underlying)
        await asyncio.sleep(0.5)
        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(underlying, "", False, False)
        for i in range(5):
            await asyncio.sleep(1.0)
            if _is_valid_price(ticker.bid) or _is_valid_price(ticker.last):
                break

    # If still nothing, try delayed-frozen (Type 4) 
    has_any = _is_valid_price(ticker.bid) or _is_valid_price(ticker.ask) or _is_valid_price(ticker.last)
    if not has_any:
        print("No delayed data. Trying delayed-frozen (Type 4)...")
        ib.cancelMktData(underlying)
        await asyncio.sleep(0.5)
        ib.reqMarketDataType(4)
        ticker = ib.reqMktData(underlying, "", False, False)
        for i in range(3):
            await asyncio.sleep(1.0)
            if _is_valid_price(ticker.bid) or _is_valid_price(ticker.last):
                break

    print(f"\n--- {symbol} Underlying Quote ---")
    bid = ticker.bid
    ask = ticker.ask
    last = ticker.last
    close_price = ticker.close

    # Indices (IND) typically have last/close but no bid/ask
    has_realtime = _is_valid_price(bid) or _is_valid_price(ask) or _is_valid_price(last) or _is_valid_price(close_price)

    print(f"  Bid:   {_fmt_price(bid)}  (size: {ticker.bidSize})")
    print(f"  Ask:   {_fmt_price(ask)}  (size: {ticker.askSize})")
    print(f"  Last:  {_fmt_price(last)}  (size: {ticker.lastSize})")
    print(f"  Close: {_fmt_price(close_price)}")

    # ── Analyze IBKR error codes ────────────────────────────────────
    error_codes = {code for code, _ in errors}
    has_competing_session = 10197 in error_codes
    has_no_subscription = 354 in error_codes
    has_farm_issue = any(c in error_codes for c in (10090, 2104, 2106, 2158))

    if errors:
        print(f"\n  ⚠ IBKR Messages (collected during all data type attempts):")
        for code, msg in errors:
            if code == 10197:
                label = "COMPETING SESSION"
            elif code == 354:
                label = "NO SUBSCRIPTION"
            elif code == 10090:
                label = "FARM CONNECTIVITY"
            elif code in (2104, 2106, 2158):
                label = "DATA FARM STATUS"
            else:
                label = "INFO"
            print(f"    [{label}] Error {code}: {msg}")

    if has_realtime:
        print(f"\n  ✓ Market data feed ACTIVE for {symbol}")
    elif has_competing_session:
        print(f"\n  ⚠ COMPETING SESSION DETECTED (Error 10197)")
        print(f"  Your subscriptions ARE active, but another TWS/Gateway")
        print(f"  session or API client is consuming the live data stream.")
        print(f"")
        print(f"  To fix this:")
        print(f"  1. Close any other TWS or IB Gateway instances")
        print(f"  2. Disconnect any other API clients")
        print(f"  3. Or: In TWS → Edit → Global Configuration → API → Settings →")
        print(f"     uncheck 'Lock Market Data for this session'")
        print(f"")
        print(f"  ✓ SUBSCRIPTION STATUS: ACTIVE (confirmed by error 10197)")
    elif has_no_subscription:
        print(f"\n  ✗ MISSING SUBSCRIPTION (Error 354)")
        print(f"  IBKR reports no market data subscription for {symbol}.")
        print(f"  → Log in to IBKR Account Management → Settings → Market Data Subscriptions")
        if sec_type.upper() in ("IND", "INDEX"):
            print(f"  → Subscribe to 'CBOE Market Data' or 'CBOE Indices'")
        print(f"  → Restart TWS/Gateway after subscription changes")
    else:
        print(f"\n  ✗ No data received for {symbol} (tried real-time, delayed, delayed-frozen)")

        now_utc = datetime.now(UTC)
        market_open = now_utc.replace(hour=14, minute=30, second=0, microsecond=0)
        market_close = now_utc.replace(hour=21, minute=0, second=0, microsecond=0)
        is_market_hours = market_open <= now_utc <= market_close
        is_weekday = now_utc.weekday() < 5

        if not is_market_hours or not is_weekday:
            print(f"  ℹ Market is currently CLOSED (UTC: {now_utc.strftime('%H:%M')}, {now_utc.strftime('%A')})")
            print(f"  This may explain missing data. Try again during market hours (9:30-4:00 ET)")
        else:
            print(f"  ✗ Market is OPEN but no data received on any data type.")
            print(f"    → Verify subscriptions in IBKR Account Management")
            if sec_type.upper() in ("IND", "INDEX"):
                print(f"    → For {symbol}: CBOE Index quotes subscription is required")
            print(f"    → Try restarting TWS/IB Gateway")

    ib.cancelMktData(underlying)

    # ── Step 3: Check option chain + near-ATM option quote ──────────
    if check_options:
        print(f"\n{'='*60}")
        print(f"  OPTION CHAIN CHECK: {symbol}")
        print(f"{'='*60}")

        # Fetch option chain parameters
        print(f"Fetching option chain parameters for {symbol}...")
        try:
            chains = await ib.reqSecDefOptParamsAsync(
                symbol, "", underlying.secType, underlying.conId
            )
        except Exception as e:
            print(f"Error fetching option chains: {e}", file=sys.stderr)
            ib.disconnect()
            return 1

        if not chains:
            print(f"✗ No option chains returned for {symbol}.")
            print(f"  → This likely means missing OPRA market data subscription.")
            ib.disconnect()
            return 1

        # Find the SMART exchange chain (or CBOE if no SMART)
        target_chain = None
        for chain in chains:
            if chain.exchange == "SMART":
                target_chain = chain
                break
        if target_chain is None:
            target_chain = chains[0]

        print(f"✓ Option chain found: exchange={target_chain.exchange}, "
              f"tradingClass={target_chain.tradingClass}, "
              f"multiplier={target_chain.multiplier}")
        print(f"  Expirations: {len(target_chain.expirations)}")
        print(f"  Strikes: {len(target_chain.strikes)}")

        # Pick the nearest expiry
        today_str = datetime.now(UTC).strftime("%Y%m%d")
        sorted_expiries = sorted(target_chain.expirations)
        nearest_expiry = None
        for exp in sorted_expiries:
            if exp >= today_str:
                nearest_expiry = exp
                break
        if not nearest_expiry and sorted_expiries:
            nearest_expiry = sorted_expiries[-1]

        print(f"  Nearest expiry: {nearest_expiry}")

        # Pick a near-ATM strike
        # Use the last/close price of the underlying as the reference
        ref_price = None
        if last is not None and last > 0:
            ref_price = last
        elif close_price is not None and close_price > 0:
            ref_price = close_price
        elif bid is not None and bid > 0 and ask is not None and ask > 0:
            ref_price = (bid + ask) / 2.0

        # Determine ATM strike: use underlying price if available, else
        # estimate from the midpoint of the strike list
        sorted_strikes = sorted(target_chain.strikes)
        if ref_price:
            atm_strike = min(sorted_strikes, key=lambda s: abs(s - ref_price))
            print(f"  Reference price: {ref_price:.2f} → ATM strike: {atm_strike}")
        elif sorted_strikes:
            # No underlying price — estimate from middle of strike range
            mid_idx = len(sorted_strikes) // 2
            atm_strike = sorted_strikes[mid_idx]
            print(f"  ⚠ No underlying price available — using mid-range strike: {atm_strike}")
            print(f"    (This is an estimate; option quote may still confirm OPRA feed)")
            ref_price = atm_strike  # sentinel
        else:
            atm_strike = None

        if atm_strike and nearest_expiry:
            # Build and qualify a call option
            opt_contract = Option(
                symbol,
                nearest_expiry,
                atm_strike,
                "C",
                target_chain.exchange,
                multiplier=str(target_chain.multiplier),
                currency=currency,
            )
            if target_chain.tradingClass:
                opt_contract.tradingClass = target_chain.tradingClass

            print(f"\nQualifying ATM call: {symbol} {nearest_expiry} {atm_strike}C...")
            try:
                opt_qualified = await ib.qualifyContractsAsync(opt_contract)
            except Exception as e:
                print(f"Error qualifying option: {e}")
                opt_qualified = []

            if opt_qualified and opt_qualified[0].conId != 0:
                opt = opt_qualified[0]
                print(f"✓ Option qualified: conId={opt.conId}, "
                      f"exchange={opt.exchange}, tradingClass={opt.tradingClass}")

                # Request option market data
                print(f"Requesting option market data...")
                ib.reqMarketDataType(1)
                opt_ticker = ib.reqMktData(opt, "", False, False)

                print("Waiting for option data (5 seconds)...")
                for i in range(5):
                    await asyncio.sleep(1.0)
                    if opt_ticker.bid is not None and opt_ticker.bid > 0:
                        break

                opt_bid = opt_ticker.bid
                opt_ask = opt_ticker.ask
                opt_last = opt_ticker.last

                print(f"\n--- {symbol} {nearest_expiry} {atm_strike}C Option Quote ---")
                print(f"  Bid:  {_fmt_price(opt_bid)}  (size: {opt_ticker.bidSize})")
                print(f"  Ask:  {_fmt_price(opt_ask)}  (size: {opt_ticker.askSize})")
                print(f"  Last: {_fmt_price(opt_last)}")

                opt_has_data = _is_valid_price(opt_bid) or _is_valid_price(opt_ask)

                if opt_has_data:
                    print(f"\n  ✓ OPRA option data feed ACTIVE")
                    spread = None
                    if _is_valid_price(opt_bid) and _is_valid_price(opt_ask):
                        spread = float(opt_ask) - float(opt_bid)
                        mid = (opt_bid + opt_ask) / 2.0
                        spread_pct = (spread / mid * 100) if mid > 0 else 0
                        print(f"  Spread: {spread:.2f} ({spread_pct:.1f}%)")
                else:
                    print(f"\n  ✗ No option market data received")
                    if not is_market_hours if 'is_market_hours' in dir() else True:
                        print(f"  ℹ Market may be closed — try during market hours")
                    print(f"    → Check OPRA (US Options) subscription in IBKR Account Management")

                # Check Greeks
                greeks = opt_ticker.modelGreeks
                if greeks and greeks.impliedVol is not None:
                    print(f"\n  --- Greeks ---")
                    print(f"  IV:    {greeks.impliedVol:.4f}")
                    print(f"  Delta: {greeks.delta:.4f}")
                    print(f"  Gamma: {greeks.gamma:.6f}")
                    print(f"  Theta: {greeks.theta:.4f}")
                    print(f"  Vega:  {greeks.vega:.4f}")
                    print(f"\n  ✓ Option greeks available")
                else:
                    print(f"\n  ℹ Greeks not available (may need active market + OPRA Top-of-Book)")

                ib.cancelMktData(opt)
            else:
                print(f"✗ Failed to qualify option contract.")
                print(f"  This may mean the expiry/strike combo is not valid or the chain is stale.")
        else:
            if not atm_strike:
                print(f"\n  ⚠ Cannot determine ATM strike — empty strike list.")
            if not nearest_expiry:
                print(f"\n  ⚠ No valid expiration found in option chain.")

    # ── Summary ─────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  SUBSCRIPTION STATUS SUMMARY")
    print(f"{'='*60}")

    sec_upper = sec_type.upper()
    if sec_upper in ("IND", "INDEX"):
        data_source = "CBOE Index"
    else:
        data_source = "NYSE/NASDAQ (via SMART)"

    if has_realtime:
        underlying_status = "✓ ACTIVE"
    elif has_competing_session:
        underlying_status = "✓ ACTIVE (competing session blocking live stream)"
    elif has_no_subscription:
        underlying_status = "✗ NO SUBSCRIPTION"
    else:
        underlying_status = "✗ NOT RECEIVING"

    print(f"  Underlying feed ({data_source}): {underlying_status}")
    if check_options:
        opt_active = locals().get('opt_has_data', False)
        opt_status = "✓ ACTIVE" if opt_active else "? NOT CHECKED / NOT RECEIVING"
        print(f"  Options feed (OPRA):              {opt_status}")
        # Option chains returned = OPRA chain metadata works
        print(f"  Option chain metadata:            ✓ AVAILABLE")

    if not has_realtime and not has_competing_session:
        print(f"\n  To fix missing data:")
        print(f"  1. Log in to IBKR Account Management → Settings → Market Data Subscriptions")
        if sec_upper in ("IND", "INDEX"):
            print(f"  2. Ensure 'CBOE Market Data' or 'CBOE Indices' is subscribed")
        print(f"  3. Ensure 'OPRA (US Options)' or 'US Options Exchanges (different tiers)' is subscribed")
        print(f"  4. Restart TWS/IB Gateway after subscription changes take effect")

    ib.disconnect()
    print(f"\nDisconnected.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnostic: Check IBKR market data feed for underlying + options. "
                    "Verifies CBOE and OPRA subscriptions."
    )
    parser.add_argument("--host", default="127.0.0.1", help="API Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7497, help="API Port (default: 7497 for TWS Paper)")
    parser.add_argument("--client-id", type=int, default=999, help="API Client ID (default: 999)")
    parser.add_argument("--symbol", default="XSP", help="Symbol to check (default: XSP)")
    parser.add_argument(
        "--sec-type", default=None,
        help="Security type override (IND, STK). Auto-detected from presets if omitted."
    )
    parser.add_argument(
        "--exchange", default=None,
        help="Exchange override (CBOE, SMART). Auto-detected from presets if omitted."
    )
    parser.add_argument("--currency", default="USD", help="Currency (default: USD)")
    parser.add_argument(
        "--no-options", action="store_true",
        help="Skip option chain and option quote checks"
    )

    args = parser.parse_args()

    # Auto-detect sec_type and exchange from presets
    symbol = args.symbol.upper()
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
            check_market_data(
                args.host,
                args.port,
                args.client_id,
                symbol,
                sec_type,
                exchange,
                args.currency,
                check_options=not args.no_options,
            )
        )
        sys.exit(code)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
