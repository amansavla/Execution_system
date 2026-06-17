"""IBKRBrokerClient implements the BrokerClient interface for Interactive Brokers.

Places limit orders only (except during emergency flatten), qualifies contracts,
tracks order status and fills, and logs raw updates to EventStore.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Callable, Optional
from uuid import UUID, uuid4

from ib_async import IB, Contract, LimitOrder, MarketOrder, Option, Stock

from src.broker.interface import BrokerClient
from src.core.enums import OrderSide, OrderStatus, PositionStatus
from src.core.models import (
    AccountState,
    FillEvent,
    OptionContract,
    OrderEvent,
    OrderPlan,
    OrderState,
    Position,
    QuoteSnapshot,
)
from src.core.config import BrokerConfig
from src.storage.event_log import EventStore

logger = logging.getLogger(__name__)

# IB to internal OrderStatus map
IB_TO_INTERNAL_STATUS = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PendingCancel": OrderStatus.CANCEL_PENDING,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
    "ApiPending": OrderStatus.SUBMITTED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "ValidationError": OrderStatus.REJECTED,
}


class IBKRBrokerClient(BrokerClient):
    """Production BrokerClient implementation using the ib_async library."""

    def __init__(self, config: BrokerConfig, event_store: EventStore) -> None:
        """Initialize the client.

        Args:
            config: Standard broker configuration.
            event_store: Real-time event log sink.
        """
        self.config = config
        self.event_store = event_store

        # Lookup structures
        self._order_callbacks: list[Callable[[OrderState, OrderEvent], None]] = []
        self._fill_callbacks: list[Callable[[FillEvent], None]] = []
        self._quote_callbacks: list[Callable[[QuoteSnapshot], None]] = []

        # Maps broker_order_id (str) -> dict of internal order metadata
        self._order_metadata: dict[str, dict] = {}
        self._order_statuses: dict[str, OrderStatus] = {}

        # Cache qualified IB contracts
        self._qualified_contracts: dict[tuple, Contract] = {}

        # Streaming market-data subscriptions: symbol -> live Ticker.
        # Quotes are served instantly from these instead of slow per-call
        # snapshot requests. Managed by get_quotes with LRU eviction.
        self._streaming_tickers: dict[str, object] = {}
        self._streaming_last_access: dict[str, float] = {}
        # Symbols that must never be evicted (e.g. the underlying index)
        self._pinned_symbols: set[str] = {"XSP"}
        self._max_streaming_subscriptions: int = 80
        self._streaming_idle_evict_seconds: float = 600.0

        # 1-min bar construction from streaming trade prints (hybrid SL +
        # bar-close signal triggers, matching backtest bar semantics).
        # Bars persist to data/bars/ for the shadow-replay acceptance test.
        from src.marketdata.bars import BarBuilder
        self.bar_builder = BarBuilder(persist_dir="data/bars")

        # Option-chain quote snapshots (data/chains/) — lets shadow-replay
        # reproduce premium-driven decisions (straddle strike selection,
        # 5EMA trail exit) that underlying bars alone can't. Throttled per
        # symbol to keep volume bounded.
        from src.marketdata.chain_log import ChainSnapshotWriter
        self.chain_writer = ChainSnapshotWriter(persist_dir="data/chains")

        # Connection management flags
        self._intentional_disconnect = False

        self.ib = IB()

        # Monkey-patch error handler to treat informational warnings (like 10349, 10148) as warnings
        # to prevent ib_async from incorrectly marking active trades as cancelled.
        original_error = self.ib.wrapper.error
        def custom_error(reqId, errorCode, errorString, advancedOrderRejectJson=""):
            if errorCode in (10349, 10148):
                logger.info(
                    "Intercepted IBKR warning code %d (reqId %d): %s. Mapping to warning code 399.",
                    errorCode, reqId, errorString
                )
                errorCode = 399
            return original_error(reqId, errorCode, errorString, advancedOrderRejectJson)
        self.ib.wrapper.error = custom_error

        # Connect event handlers
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_exec_details
        self.ib.pendingTickersEvent += self._on_pending_ticker
        self.ib.disconnectedEvent += self._on_disconnected

    # -----------------------------------------------------------------------
    # Connection Management
    # -----------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish connection to TWS/Gateway socket."""
        self._intentional_disconnect = False

        # 1. Pre-connection safety gates
        if self.config.live_trading.enabled:
            import os
            env_val = os.environ.get("ALLOW_LIVE_TRADING")
            if env_val != "I_UNDERSTAND_THIS_CAN_LOSE_MONEY":
                raise ValueError(
                    "Live trading safety gate failed: environment variable "
                    "ALLOW_LIVE_TRADING must be set to 'I_UNDERSTAND_THIS_CAN_LOSE_MONEY' "
                    f"(currently: {repr(env_val)})"
                )

            # Check allowlist is configured
            if not self.config.account.allowlist:
                raise ValueError(
                    "Live trading safety gate failed: account.allowlist must be non-empty "
                    "in broker.yaml when live trading is enabled"
                )

            # If a specific account_id is configured, check if it's in the allowlist
            if self.config.account.account_id and self.config.account.account_id not in self.config.account.allowlist:
                raise ValueError(
                    f"Live trading safety gate failed: configured account_id '{self.config.account.account_id}' "
                    "is not present in the account allowlist"
                )

        logger.info(
            "Connecting to IBKR at %s:%d with client ID %d...",
            self.config.connection.host,
            self.config.connection.port,
            self.config.connection.client_id,
        )
        try:
            await asyncio.wait_for(
                self.ib.connectAsync(
                    self.config.connection.host,
                    self.config.connection.port,
                    clientId=self.config.connection.client_id,
                ),
                timeout=float(self.config.connection.timeout_seconds),
            )
            logger.info("Connected to IBKR successfully.")
        except Exception as e:
            logger.error("Failed to connect to IBKR: %s", e)
            raise

        # 2. Post-connection safety gates
        accounts = self.ib.managedAccounts()

        if self.config.live_trading.enabled:
            # Verify that managed accounts are returned
            if not accounts:
                self.ib.disconnect()
                raise RuntimeError(
                    "Live trading safety gate failed: No managed accounts returned by broker connection."
                )

            # If account_id is configured, make sure it is in managed accounts
            if self.config.account.account_id and self.config.account.account_id not in accounts:
                self.ib.disconnect()
                raise ValueError(
                    f"Live trading safety gate failed: Configured account_id '{self.config.account.account_id}' "
                    f"is not in broker managed accounts: {accounts}"
                )

            # Verify that the active account is allowed
            if self.config.account.account_id:
                if self.config.account.account_id not in self.config.account.allowlist:
                    self.ib.disconnect()
                    raise ValueError(
                        f"Live trading safety gate failed: Account ID '{self.config.account.account_id}' is not in allowlist"
                    )
            else:
                for acc in accounts:
                    if acc not in self.config.account.allowlist:
                        self.ib.disconnect()
                        raise ValueError(
                            f"Live trading safety gate failed: Account ID '{acc}' is not in allowlist"
                        )
        else:
            # If live trading is disabled, verify that no live accounts are connected
            if accounts:
                is_paper = all(acc.upper().startswith(("DU", "DF")) for acc in accounts)
                if not is_paper:
                    live_accounts = [acc for acc in accounts if not acc.upper().startswith(("DU", "DF"))]
                    self.ib.disconnect()
                    raise RuntimeError(
                        f"Live connection detected: Managed accounts {live_accounts} are LIVE, "
                        "but live_trading.enabled is False in config. Disconnecting immediately."
                    )


    async def disconnect(self) -> None:
        """Disconnect cleanly from the API socket."""
        self._intentional_disconnect = True
        logger.info("Disconnecting from IBKR API socket...")
        self.ib.disconnect()

    async def is_connected(self) -> bool:
        """Check socket connection state."""
        return self.ib.isConnected()

    def _on_disconnected(self) -> None:
        """Handle unexpected disconnects and trigger auto-reconnect."""
        # Streaming tickers die with the socket; drop them so get_quotes
        # lazily resubscribes after reconnection.
        self._streaming_tickers.clear()
        self._streaming_last_access.clear()

        if not self._intentional_disconnect and self.config.reconnection.enabled:
            logger.warning("Unexpectedly disconnected from IBKR. Triggering auto-reconnect...")
            # Hold a strong reference: asyncio only weak-refs tasks, and a
            # GC'd reconnect task would silently strand the system offline.
            self._reconnect_task = asyncio.create_task(self._auto_reconnect())

    async def _auto_reconnect(self) -> None:
        """Reconnection loop trying to re-establish API connection."""
        retries = 0
        max_retries = self.config.reconnection.max_retries
        delay = self.config.reconnection.retry_delay_seconds

        while retries < max_retries and not self.ib.isConnected():
            retries += 1
            logger.info(
                "Attempting auto-reconnection (%d/%d) in %d seconds...",
                retries,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            try:
                await self.ib.connectAsync(
                    self.config.connection.host,
                    self.config.connection.port,
                    clientId=self.config.connection.client_id,
                )
                logger.info("Reconnection successful.")
                return
            except Exception as e:
                logger.error("Reconnection attempt %d failed: %s", retries, e)

        logger.critical("Auto-reconnection failed after %d retries.", retries)

    # -----------------------------------------------------------------------
    # Order Routing and Modification
    # -----------------------------------------------------------------------

    async def place_order(self, order_plan: OrderPlan, emergency_flatten: bool = False) -> OrderState:
        """Submit a limit order to the broker.

        Raises:
            RuntimeError: If attempting to submit to a live session when live trading is disabled.
            ValueError: If a market order is submitted and emergency_flatten is False.
        """
        if not await self.is_connected():
            raise ConnectionError("Cannot place order: Broker client is disconnected.")

        # Safety Gate: verify session is paper vs live
        accounts = self.ib.managedAccounts()
        is_paper = all(acc.upper().startswith(("DU", "DF")) for acc in accounts) if accounts else True

        if not is_paper:
            if not self.config.live_trading.enabled:
                raise RuntimeError(
                    "Submission blocked: Connected account is LIVE, but live_trading.enabled is False in config."
                )

            # Enforce env var check at order routing time as well
            import os
            if os.environ.get("ALLOW_LIVE_TRADING") != "I_UNDERSTAND_THIS_CAN_LOSE_MONEY":
                raise ValueError(
                    "Submission blocked: Environment variable ALLOW_LIVE_TRADING must be set to 'I_UNDERSTAND_THIS_CAN_LOSE_MONEY'"
                )

            # Enforce allowlist check at order routing time as well
            if self.config.account.account_id:
                if self.config.account.account_id not in self.config.account.allowlist:
                    raise ValueError(
                        f"Submission blocked: Account ID '{self.config.account.account_id}' is not in allowlist"
                    )
            else:
                for acc in accounts:
                    if acc not in self.config.account.allowlist:
                        raise ValueError(
                            f"Submission blocked: Account ID '{acc}' is not in allowlist"
                        )


        # Qualify OptionContract
        ib_contract = await self._qualify_option_contract(order_plan.contract)

        action = "BUY" if order_plan.side == OrderSide.BUY else "SELL"

        # Limit order constraint
        limit_price = order_plan.limit_price
        if order_plan.order_type == "LMT":
            # Round price to conform to minimum price variation (Penny Pilot rules: 0.01 under 3.00, 0.05 above)
            raw_price = order_plan.limit_price
            if raw_price < 3.00:
                limit_price = round(raw_price, 2)
            else:
                limit_price = round(raw_price * 20.0) / 20.0
            
            logger.info("Rounding limit price from %s to %s for %s", raw_price, limit_price, order_plan.contract.symbol)
            ib_order = LimitOrder(action, order_plan.quantity, limit_price)
        elif order_plan.order_type == "MKT" and emergency_flatten:
            ib_order = MarketOrder(action, order_plan.quantity)
        else:
            raise ValueError(
                f"Order type {order_plan.order_type} not supported except when emergency_flatten is enabled."
            )

        ib_order.tif = order_plan.time_in_force
        ib_order.transmit = True
        # Eligibility for Cboe GTH (XSP/SPX/VIX trade 20:15-09:15 ET):
        # orders placed outside regular hours must carry outsideRth or IBKR
        # queues them for the next RTH open instead of the live GTH book.
        if not self._is_regular_trading_hours():
            ib_order.outsideRth = True
        # Audit trail: shows up in TWS / Flex reports / executions
        if order_plan.order_ref:
            ib_order.orderRef = order_plan.order_ref

        # IBKR Adaptive algo: works the LIMIT order inside the spread for
        # faster fills at better all-in prices (single-leg only, RTH only —
        # outside regular hours IBKR rejects algo orders, so we silently
        # fall back to the plain limit).
        if (
            order_plan.algo
            and order_plan.order_type == "LMT"
            and self._is_regular_trading_hours()
        ):
            from ib_async import TagValue
            priority = {
                "adaptive_urgent": "Urgent",
                "adaptive_normal": "Normal",
                "adaptive_patient": "Patient",
            }.get(order_plan.algo)
            if priority:
                ib_order.algoStrategy = "Adaptive"
                ib_order.algoParams = [TagValue("adaptivePriority", priority)]

        order_id = uuid4()
        if not ib_order.orderId:
            ib_order.orderId = self.ib.client.getReqId()
        broker_order_id = str(ib_order.orderId)

        # Save metadata mapping for callback resolution BEFORE placing the order to avoid race conditions
        self._order_metadata[broker_order_id] = {
            "order_id": order_id,
            "order_plan_id": order_plan.order_plan_id,
            "position_id": order_plan.position_id,
            "is_entry": order_plan.is_entry,
            "strategy_id": order_plan.strategy_id,
            "contract": order_plan.contract,
            "side": order_plan.side,
            "limit_price": limit_price,
            "quantity": order_plan.quantity,
            "created_at": datetime.now(UTC),
        }

        # Place the order (non-blocking call returning Trade)
        trade = self.ib.placeOrder(ib_contract, ib_order)

        # Build initial state
        return OrderState(
            order_id=order_id,
            order_plan_id=order_plan.order_plan_id,
            position_id=order_plan.position_id,
            is_entry=order_plan.is_entry,
            strategy_id=order_plan.strategy_id,
            contract=order_plan.contract,
            side=order_plan.side,
            quantity=order_plan.quantity,
            filled_quantity=0,
            limit_price=limit_price,
            status=OrderStatus.NEW,
            broker_order_id=broker_order_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order."""
        if not await self.is_connected():
            raise ConnectionError("Cannot cancel order: Broker client is disconnected.")

        # Sync open orders with TWS to ensure trades cache is up-to-date
        try:
            await self.ib.reqOpenOrdersAsync()
        except Exception as e:
            logger.warning("Failed to sync open orders before cancellation: %s", e)

        for trade in self.ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                self.ib.cancelOrder(trade.order)
                return True

        logger.warning("Order with broker order ID %s not found in trades cache.", broker_order_id)
        return False

    async def get_order_status(self, broker_order_id: str) -> Optional[OrderStatus]:
        """Ground-truth status of an order from the broker's trades cache.

        Used by OrderManager's stuck-cancel sweep: an order left in
        CANCEL_PENDING whose confirmation callback was lost gets resolved
        from here. Returns None when the broker does not know the order.
        """
        if not await self.is_connected():
            return None
        for trade in self.ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                status = IB_TO_INTERNAL_STATUS.get(
                    trade.orderStatus.status, OrderStatus.SUBMITTED
                )
                if trade.orderStatus.remaining == 0 and trade.orderStatus.filled > 0:
                    status = OrderStatus.FILLED
                return status
        return None

    async def modify_order(self, broker_order_id: str, new_limit_price: float) -> bool:
        """Modify a working limit order's price IN PLACE (no cancel/replace).

        Re-placing the same order object with an updated lmtPrice instructs
        IBKR to amend the existing order — faster than cancel/replace and
        preserves queue priority where possible.
        """
        if not await self.is_connected():
            raise ConnectionError("Cannot modify order: Broker client is disconnected.")

        for trade in self.ib.trades():
            if str(trade.order.orderId) == broker_order_id:
                # Refuse to modify orders that are done or being cancelled —
                # a modify racing a cancel draws IBKR Error 201.
                if trade.isDone() or trade.orderStatus.status in (
                    "PendingCancel", "ApiCancelled", "Cancelled", "Inactive",
                ):
                    return False
                # Conform to minimum price variation (penny rules)
                if new_limit_price < 3.00:
                    px = round(new_limit_price, 2)
                else:
                    px = round(new_limit_price * 20.0) / 20.0
                trade.order.lmtPrice = px
                self.ib.placeOrder(trade.contract, trade.order)
                # Keep metadata limit price in sync for callbacks
                meta = self._order_metadata.get(broker_order_id)
                if meta is not None:
                    meta["limit_price"] = px
                logger.info(
                    "Modified order %s limit price in place to %.2f",
                    broker_order_id, px,
                )
                return True

        logger.warning("modify_order: broker order ID %s not found in trades cache.", broker_order_id)
        return False

    # -----------------------------------------------------------------------
    # Query APIs
    # -----------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        """Fetch all open positions from the broker account."""
        if not await self.is_connected():
            raise ConnectionError("Cannot fetch positions: Broker client is disconnected.")

        positions = []
        for p in self.ib.positions():
            opt_contract = self._from_ib_contract(p.contract)
            qty = int(p.position)
            side = OrderSide.BUY if qty > 0 else OrderSide.SELL

            # IBKR's avgCost for options is reported per-contract including the
            # multiplier (e.g. 258.23 for a 2.58 option), but internal pricing
            # (fill_price, average_entry_price, PnL math) is per-share/contract
            # unit price. Normalize by dividing out the multiplier.
            multiplier = opt_contract.multiplier if opt_contract.multiplier else 100
            avg_price = p.avgCost / multiplier if multiplier else p.avgCost

            positions.append(
                Position(
                    position_id=uuid4(),
                    strategy_id="unknown",
                    contract=opt_contract,
                    side=side,
                    quantity=abs(qty),
                    average_entry_price=avg_price,
                    status=PositionStatus.OPEN,
                    entry_order_id=uuid4(),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        return positions

    async def get_open_orders(self) -> list[OrderState]:
        """Fetch all active open trades."""
        if not await self.is_connected():
            raise ConnectionError("Cannot fetch open orders: Broker client is disconnected.")

        active_statuses = {
            OrderStatus.NEW,
            OrderStatus.RISK_CHECKED,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.CANCEL_PENDING,
        }
        open_orders = []
        for trade in self.ib.openTrades():
            broker_order_id = str(trade.order.orderId)
            metadata = self._order_metadata.get(broker_order_id, {})
            order_state = self._to_internal_order_state(trade, metadata)
            if order_state.status in active_statuses:
                open_orders.append(order_state)
        return open_orders

    async def get_account_state(self) -> AccountState:
        """Fetch current balances and liquidation value for configured account."""
        if not await self.is_connected():
            raise ConnectionError("Cannot fetch account state: Broker client is disconnected.")

        accounts = self.ib.managedAccounts()
        acc_id = self.config.account.account_id or (accounts[0] if accounts else "unknown")

        net_liq = 0.0
        avail_funds = 0.0
        buying_power = 0.0

        # Try accountSummary cache first
        summary = await self.ib.accountSummaryAsync(acc_id)
        for item in summary:
            if item.tag == "NetLiquidation":
                net_liq = float(item.value)
            elif item.tag == "AvailableFunds":
                avail_funds = float(item.value)
            elif item.tag == "BuyingPower":
                buying_power = float(item.value)

        # Fallback to accountValues if summary is blank
        if net_liq == 0.0 and avail_funds == 0.0:
            for item in self.ib.accountValues():
                if item.account != acc_id:
                    continue
                if item.tag == "NetLiquidation":
                    try:
                        net_liq = float(item.value)
                    except ValueError:
                        pass
                elif item.tag == "AvailableFunds":
                    try:
                        avail_funds = float(item.value)
                    except ValueError:
                        pass
                elif item.tag == "BuyingPower":
                    try:
                        buying_power = float(item.value)
                    except ValueError:
                        pass

        return AccountState(
            account_id=acc_id,
            net_liquidation=net_liq,
            available_funds=avail_funds,
            buying_power=buying_power,
            timestamp=datetime.now(UTC),
        )

    async def get_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        """Fetch snapshots of current market quotes."""
        if not await self.is_connected():
            raise ConnectionError("Cannot fetch quotes: Broker client is disconnected.")

        result = {}
        contracts_to_query = []

        for sym in symbols:
            ib_contract = self._parse_symbol_to_ib_contract(sym)
            contracts_to_query.append((sym, ib_contract))

        # Qualify all contracts first (cached & concurrent)
        if not hasattr(self, "_qualified_by_symbol"):
            self._qualified_by_symbol = {}

        qualified_pairs = []
        contracts_to_qualify = []

        for sym, ib_contract in contracts_to_query:
            if sym in self._qualified_by_symbol:
                qualified_pairs.append((sym, self._qualified_by_symbol[sym]))
            else:
                contracts_to_qualify.append((sym, ib_contract))

        if contracts_to_qualify:
            async def qualify_with_timeout(contract):
                return await asyncio.wait_for(self.ib.qualifyContractsAsync(contract), timeout=3.0)
            tasks = [qualify_with_timeout(c) for _, c in contracts_to_qualify]
            qualified_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (sym, ib_contract), qualified in zip(contracts_to_qualify, qualified_results):
                if isinstance(qualified, Exception):
                    logger.error("Failed to qualify contract for symbol %s: %s", sym, qualified)
                elif qualified and qualified[0] is not None:
                    target_contract = qualified[0]
                    self._qualified_by_symbol[sym] = target_contract
                    qualified_pairs.append((sym, target_contract))

        # Request market data via persistent streaming subscriptions.
        # Already-subscribed symbols are served instantly from the live
        # ticker; only brand-new symbols pay a short warm-up wait.
        self.ib.reqMarketDataType(1)  # Default real-time
        now_mono = asyncio.get_event_loop().time()

        active_tickers = []
        new_tickers = []
        for sym, qualified_contract in qualified_pairs:
            ticker = self._streaming_tickers.get(sym)
            if ticker is None:
                # streaming subscription (snapshot=False) — stays live
                ticker = self.ib.reqMktData(qualified_contract, "", False, False)
                self._streaming_tickers[sym] = ticker
                new_tickers.append(ticker)
            self._streaming_last_access[sym] = now_mono
            active_tickers.append((sym, qualified_contract, ticker))

        # Warm-up: wait only for NEW subscriptions to receive their first
        # bid/ask, polling instead of a fixed sleep (max 2s).
        if new_tickers:
            for _ in range(20):
                if all(
                    (t.bid is not None and t.bid >= 0)
                    or (t.ask is not None and t.ask >= 0)
                    or (t.last is not None and t.last >= 0)
                    for t in new_tickers
                ):
                    break
                await asyncio.sleep(0.1)

        # Evict idle, unpinned subscriptions (LRU) to respect IBKR's
        # market-data line limit.
        self._evict_idle_subscriptions(now_mono)

        for sym, qualified_contract, ticker in active_tickers:
            bid = float(ticker.bid) if ticker.bid is not None and ticker.bid >= 0 else None
            ask = float(ticker.ask) if ticker.ask is not None and ticker.ask >= 0 else None
            last = float(ticker.last) if ticker.last is not None and ticker.last >= 0 else None
            volume = int(ticker.volume) if ticker.volume is not None and ticker.volume >= 0 else None

            delta, gamma, vega, theta, iv = None, None, None, None, None
            if ticker.modelGreeks:
                delta = float(ticker.modelGreeks.delta) if ticker.modelGreeks.delta is not None else None
                gamma = float(ticker.modelGreeks.gamma) if ticker.modelGreeks.gamma is not None else None
                vega = float(ticker.modelGreeks.vega) if ticker.modelGreeks.vega is not None else None
                theta = float(ticker.modelGreeks.theta) if ticker.modelGreeks.theta is not None else None
                iv = float(ticker.modelGreeks.impliedVol) if ticker.modelGreeks.impliedVol is not None else None

            close = float(ticker.close) if ticker.close is not None and ticker.close >= 0 else None

            # Use the ticker's own last-update time so downstream
            # quote-freshness checks see true staleness, not read time.
            ts = datetime.now(UTC)
            ticker_time = getattr(ticker, "time", None)
            if ticker_time is not None:
                ts = ticker_time if ticker_time.tzinfo else ticker_time.replace(tzinfo=UTC)

            snapshot = QuoteSnapshot(
                symbol=sym,
                bid=bid,
                ask=ask,
                last=last,
                volume=volume,
                delta=delta,
                close=close,
                gamma=gamma,
                vega=vega,
                theta=theta,
                implied_volatility=iv,
                timestamp=ts,
            )
            result[sym] = snapshot

        # Snapshot option quotes for shadow-replay (throttled, best-effort).
        self.chain_writer.record(result, now_mono)

        return result

    def _evict_idle_subscriptions(self, now_mono: float) -> None:
        """Cancel streaming market-data lines that are idle or over the cap.

        Pinned symbols (e.g. the underlying) are never evicted. Eviction is
        LRU by last get_quotes access time.
        """
        evictable = [
            (sym, last)
            for sym, last in self._streaming_last_access.items()
            if sym not in self._pinned_symbols and sym in self._streaming_tickers
        ]

        to_evict: set[str] = set()

        # 1. Idle timeout
        for sym, last in evictable:
            if (now_mono - last) > self._streaming_idle_evict_seconds:
                to_evict.add(sym)

        # 2. Hard cap (LRU)
        live_count = len(self._streaming_tickers) - len(to_evict)
        if live_count > self._max_streaming_subscriptions:
            survivors = sorted(
                (e for e in evictable if e[0] not in to_evict),
                key=lambda e: e[1],
            )
            overflow = live_count - self._max_streaming_subscriptions
            for sym, _ in survivors[:overflow]:
                to_evict.add(sym)

        for sym in to_evict:
            ticker = self._streaming_tickers.pop(sym, None)
            self._streaming_last_access.pop(sym, None)
            if ticker is not None and getattr(ticker, "contract", None) is not None:
                try:
                    self.ib.cancelMktData(ticker.contract)
                except Exception as e:
                    logger.debug("cancelMktData failed for %s: %s", sym, e)
        if to_evict:
            logger.debug("Evicted %d idle market-data subscriptions", len(to_evict))

    # -----------------------------------------------------------------------
    # Callback Event Handlers
    # -----------------------------------------------------------------------

    def register_order_callback(
        self, callback: Callable[[OrderState, OrderEvent], None]
    ) -> None:
        self._order_callbacks.append(callback)

    def register_fill_callback(self, callback: Callable[[FillEvent], None]) -> None:
        self._fill_callbacks.append(callback)

    def register_quote_callback(self, callback: Callable[[QuoteSnapshot], None]) -> None:
        self._quote_callbacks.append(callback)

    async def get_historical_close(self, symbol: str, end_time: datetime) -> Optional[float]:
        """Fetch historical close price for a symbol ending at the specified datetime."""
        if not self.ib.isConnected():
            raise ConnectionError("IBKRBrokerClient is not connected")

        # Convert symbol to IB contract
        ib_contract = self._parse_symbol_to_ib_contract(symbol)
        qualified = await self.ib.qualifyContractsAsync(ib_contract)
        if not qualified:
            logger.warning("Failed to qualify contract for symbol %s", symbol)
            return None
        contract = qualified[0]

        # Format end_time for IBKR: YYYYMMDD HH:mm:ss timezone
        if end_time.tzinfo is not None:
            utc_time = end_time.astimezone(UTC)
            end_str = utc_time.strftime("%Y%m%d %H:%M:%S") + " UTC"
        else:
            end_str = end_time.strftime("%Y%m%d %H:%M:%S") + " UTC"

        try:
            # Fetch 1 minute bar ending at end_time
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_str,
                durationStr="60 S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if bars:
                logger.info("Fetched historical bar for %s at %s: close=%f", symbol, end_str, bars[-1].close)
                return bars[-1].close
            logger.warning("No historical bars returned for %s at %s", symbol, end_str)
            return None
        except Exception as e:
            logger.error("Error fetching historical data for %s: %s", symbol, e)
            return None

    def _on_order_status(self, trade) -> None:
        """Process incoming order status update from IBKR."""
        broker_order_id = str(trade.order.orderId)
        metadata = self._order_metadata.get(broker_order_id)
        if not metadata:
            return

        # Log raw broker callback payload to EventStore
        raw_payload = {
            "orderId": trade.order.orderId,
            "permId": trade.order.permId,
            "status": trade.orderStatus.status,
            "filled": trade.orderStatus.filled,
            "remaining": trade.orderStatus.remaining,
            "avgFillPrice": trade.orderStatus.avgFillPrice,
            "lastFillPrice": trade.orderStatus.lastFillPrice,
            "whyHeld": trade.orderStatus.whyHeld,
        }
        self.event_store.log_callback("order_callback", raw_payload)

        # Convert status
        prev_status = self._order_statuses.get(broker_order_id)
        new_status = IB_TO_INTERNAL_STATUS.get(trade.orderStatus.status, OrderStatus.SUBMITTED)
        if new_status == OrderStatus.SUBMITTED and trade.orderStatus.filled > 0 and trade.orderStatus.remaining > 0:
            new_status = OrderStatus.PARTIALLY_FILLED

        if prev_status == new_status:
            return

        self._order_statuses[broker_order_id] = new_status

        # Convert to internal models
        order_state = self._to_internal_order_state(trade, metadata)
        order_event = OrderEvent(
            order_id=metadata["order_id"],
            previous_status=prev_status,
            new_status=new_status,
            message=f"IBKR status change to {trade.orderStatus.status}",
            timestamp=datetime.now(UTC),
        )

        self.event_store.log_callback("order_event", order_event)

        for cb in self._order_callbacks:
            try:
                cb(order_state, order_event)
            except Exception as e:
                logger.error("Error in order callback execution: %s", e)

    def _on_exec_details(self, trade, fill) -> None:
        """Process incoming execution (fill) update from IBKR."""
        broker_order_id = str(fill.execution.orderId)
        metadata = self._order_metadata.get(broker_order_id)
        if not metadata:
            return

        # Log raw callback details to EventStore
        raw_payload = {
            "orderId": fill.execution.orderId,
            "execId": fill.execution.execId,
            "shares": fill.execution.shares,
            "price": fill.execution.price,
            "cumQty": fill.execution.cumQty,
            "side": fill.execution.side,
            "time": str(fill.execution.time),
        }
        self.event_store.log_callback("fill_callback", raw_payload)

        # Convert to internal model
        fill_event = FillEvent(
            fill_id=uuid4(),
            order_id=metadata["order_id"],
            strategy_id=metadata["strategy_id"],
            contract=metadata["contract"],
            side=metadata["side"],
            filled_quantity=fill.execution.shares,
            fill_price=fill.execution.price,
            commission=None,
            timestamp=datetime.now(UTC),
            # broker_order_id lets OrderManager correlate this fill back to
            # ITS OrderState (execution-quality / slippage instrumentation)
            metadata={"broker_order_id": str(fill.execution.orderId)},
        )

        self.event_store.log_callback("fill_event", fill_event)

        for cb in self._fill_callbacks:
            try:
                cb(fill_event)
            except Exception as e:
                logger.error("Error in fill callback execution: %s", e)

    @staticmethod
    def _is_regular_trading_hours() -> bool:
        """True during US regular trading hours (9:30-16:00 NY, Mon-Fri)."""
        from zoneinfo import ZoneInfo
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        if now_ny.weekday() >= 5:
            return False
        minutes = now_ny.hour * 60 + now_ny.minute
        return (9 * 60 + 30) <= minutes < (16 * 60)

    def _internal_symbol_for_ib_contract(self, ib_contract) -> str:
        """Map an IB contract to the internal quote-symbol convention."""
        try:
            sec_type = getattr(ib_contract, "secType", "")
            if sec_type == "OPT":
                return self._from_ib_contract(ib_contract).to_quote_symbol()
        except Exception:
            pass
        return ib_contract.symbol

    def _on_pending_ticker(self, tickers) -> None:
        """Process incoming ticker data updates from streaming connections.

        Note: ib_async's pendingTickersEvent emits a SET of tickers per
        batch (not a single ticker). Handle both for safety.
        """
        if not isinstance(tickers, (set, list, tuple)):
            tickers = [tickers]

        for ticker in tickers:
            try:
                contract = getattr(ticker, "contract", None)
                if contract is None:
                    continue

                bid = float(ticker.bid) if ticker.bid is not None and ticker.bid >= 0 else None
                ask = float(ticker.ask) if ticker.ask is not None and ticker.ask >= 0 else None
                last = float(ticker.last) if ticker.last is not None and ticker.last >= 0 else None
                volume = int(ticker.volume) if ticker.volume is not None and ticker.volume >= 0 else None

                internal_sym = self._internal_symbol_for_ib_contract(contract)

                # Feed the 1-min bar builder with traded prices (backtest
                # bars are OHLC of trades). For the index underlying there
                # are no trades — use the computed index level ('last' on
                # IND tickers; falls back to mid).
                bar_price = last
                if bar_price is None and getattr(contract, "secType", "") == "IND":
                    if bid is not None and ask is not None:
                        bar_price = (bid + ask) / 2.0
                if bar_price is not None:
                    ts = getattr(ticker, "time", None)
                    self.bar_builder.on_tick(internal_sym, bar_price, ts)

                delta, gamma, vega, theta, iv = None, None, None, None, None
                if ticker.modelGreeks:
                    delta = float(ticker.modelGreeks.delta) if ticker.modelGreeks.delta is not None else None
                    gamma = float(ticker.modelGreeks.gamma) if ticker.modelGreeks.gamma is not None else None
                    vega = float(ticker.modelGreeks.vega) if ticker.modelGreeks.vega is not None else None
                    theta = float(ticker.modelGreeks.theta) if ticker.modelGreeks.theta is not None else None
                    iv = float(ticker.modelGreeks.impliedVol) if ticker.modelGreeks.impliedVol is not None else None

                close = float(ticker.close) if ticker.close is not None and ticker.close >= 0 else None

                snapshot = QuoteSnapshot(
                    symbol=internal_sym,
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=volume,
                    delta=delta,
                    close=close,
                    gamma=gamma,
                    vega=vega,
                    theta=theta,
                    implied_volatility=iv,
                    timestamp=datetime.now(UTC),
                )

                for cb in self._quote_callbacks:
                    try:
                        cb(snapshot)
                    except Exception as e:
                        logger.error("Error in quote callback execution: %s", e)
            except Exception as e:
                logger.debug("Error processing pending ticker: %s", e)

    # -----------------------------------------------------------------------
    # 1-minute bars (built from streaming trade prints)
    # -----------------------------------------------------------------------

    def get_latest_completed_bar(self, symbol: str):
        """Most recent completed 1-min bar for an internal quote symbol."""
        return self.bar_builder.get_latest_completed_bar(symbol)

    def get_completed_bars(self, symbol: str, count: int = 60):
        """Up to `count` most recent completed 1-min bars (oldest first)."""
        return self.bar_builder.get_completed_bars(symbol, count)

    # -----------------------------------------------------------------------
    # Helper Methods
    # -----------------------------------------------------------------------

    async def _qualify_option_contract(self, contract: OptionContract) -> Contract:
        """Qualify the contract with IBKR to populate identifiers."""
        ib_contract = self._to_ib_contract(contract)
        contract_key = (contract.symbol, contract.expiry, contract.strike, contract.right)

        if contract_key in self._qualified_contracts:
            return self._qualified_contracts[contract_key]

        qualified = await self.ib.qualifyContractsAsync(ib_contract)
        if not qualified or qualified[0] is None:
            raise ValueError(f"Failed to qualify contract: {contract}")

        target_contract = qualified[0]
        self._qualified_contracts[contract_key] = target_contract
        return target_contract

    def _to_ib_contract(self, contract: OptionContract) -> Contract:
        """Convert internal OptionContract model to ib_async contract."""
        right_str = contract.right.value if hasattr(contract.right, "value") else str(contract.right)
        right_char = "C" if "CALL" in right_str.upper() or right_str.upper() == "C" else "P"
        return Option(
            symbol=contract.symbol,
            lastTradeDateOrContractMonth=contract.expiry,
            strike=float(contract.strike),
            right=right_char,
            exchange="SMART",
            currency="USD",
        )

    def _from_ib_contract(self, ib_contract) -> OptionContract:
        """Convert ib_async contract back to internal OptionContract model."""
        from src.core.enums import OptionRight

        right_str = getattr(ib_contract, "right", "") or ""
        right = OptionRight.CALL if right_str.upper().startswith("C") else OptionRight.PUT
        expiry = getattr(ib_contract, "lastTradeDateOrContractMonth", "") or ""
        strike = getattr(ib_contract, "strike", 0.0) or 0.0

        return OptionContract(
            symbol=ib_contract.symbol or "",
            expiry=str(expiry),
            strike=float(strike),
            right=right,
        )

    def _parse_symbol_to_ib_contract(self, sym: str) -> Contract:
        """Convert symbol string back to standard IB contract."""
        from ib_async import Index

        parts = sym.split()
        if len(parts) >= 4:
            underlying = parts[0]
            expiry = parts[1]
            try:
                strike = float(parts[2])
                right_str = parts[3]
            except ValueError:
                try:
                    right_str = parts[2]
                    strike = float(parts[3])
                except ValueError:
                    return Stock(sym, "SMART", "USD")

            right_char = "C" if right_str.upper().startswith("C") else "P"
            return Option(underlying, expiry, strike, right_char, "SMART", currency="USD")

        if sym.upper() in ("SPX", "NDX", "RUT", "XSP"):
            return Index(sym, "CBOE", "USD")
        return Stock(sym, "SMART", "USD")

    def _to_internal_order_state(self, trade, metadata: dict) -> OrderState:
        """Convert ib_async trade to internal OrderState model."""
        broker_order_id = str(trade.order.orderId)
        ib_status = trade.orderStatus.status
        status = IB_TO_INTERNAL_STATUS.get(ib_status, OrderStatus.SUBMITTED)
        if trade.orderStatus.remaining == 0 and trade.orderStatus.filled > 0:
            status = OrderStatus.FILLED
        elif status == OrderStatus.SUBMITTED and trade.orderStatus.filled > 0 and trade.orderStatus.remaining > 0:
            status = OrderStatus.PARTIALLY_FILLED

        order_id = metadata.get("order_id") or uuid4()
        order_plan_id = metadata.get("order_plan_id") or uuid4()
        position_id = metadata.get("position_id")
        is_entry = metadata.get("is_entry") or True
        strategy_id = metadata.get("strategy_id") or "unknown"

        opt_contract = self._from_ib_contract(trade.contract)
        side = OrderSide.BUY if trade.order.action.upper() == "BUY" else OrderSide.SELL

        return OrderState(
            order_id=order_id,
            order_plan_id=order_plan_id,
            position_id=position_id,
            is_entry=is_entry,
            strategy_id=strategy_id,
            contract=opt_contract,
            side=side,
            quantity=trade.order.totalQuantity,
            filled_quantity=trade.orderStatus.filled,
            limit_price=trade.order.lmtPrice,
            status=status,
            broker_order_id=broker_order_id,
            created_at=metadata.get("created_at") or datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
