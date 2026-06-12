#!/usr/bin/env python3
"""Live order/cancel shakeout against TWS paper — runs OFF-HOURS too.

Exercises the exact failure modes from the 2026-06-11 incidents without
needing a fillable market:

  1. place a deep-OTM limit order (will not fill) -> expect SUBMITTED/
     PreSubmitted callback
  2. cancel it -> expect a CANCELLED confirmation within N seconds
  3. cancel/replace chain (place -> cancel -> place replacement), the
     repricer's path -> expect both legs to resolve
  4. stuck-cancel probe: cancel and verify get_order_status() agrees

Uses its own client_id (default 17) so the running paper system on
client 9 is untouched. Orders are 1-lot XSP options priced at 0.01 —
they cannot fill anywhere near the market.

Usage:
    python3 scripts/live_cancel_shakeout.py            # next-session expiry
    python3 scripts/live_cancel_shakeout.py 20260612   # explicit expiry
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from src.broker.ibkr_broker import IBKRBrokerClient
from src.core.config import load_broker_config
from src.core.enums import OptionRight, OrderSide, OrderStatus
from src.core.models import OptionContract, OrderPlan
from src.storage.event_log import EventStore
from pathlib import Path
from uuid import uuid4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for noisy in ("ib_async.client", "ib_async.wrapper"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("shakeout")

CLIENT_ID = 17
CANCEL_CONFIRM_TIMEOUT = 20.0


def next_session_expiry() -> str:
    ny = datetime.now(ZoneInfo("America/New_York"))
    d = ny.date()
    if ny.hour >= 16:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


async def wait_status(broker, broker_order_id, targets, timeout):
    """Poll the broker's ground truth until status is in targets."""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = await broker.get_order_status(broker_order_id)
        if last in targets:
            return last
        await asyncio.sleep(0.5)
    return last


async def main():
    expiry = sys.argv[1] if len(sys.argv) > 1 else next_session_expiry()

    cfg = load_broker_config(Path("configs/broker.yaml"))
    cfg.connection.client_id = CLIENT_ID
    store = EventStore(db_path=":memory:")
    await store.start()
    broker = IBKRBrokerClient(cfg, store)

    statuses: list[tuple[str, str]] = []
    broker.register_order_callback(
        lambda order, event: statuses.append(
            (order.broker_order_id, str(event.new_status))
        )
    )

    await broker.connect()
    log.info("Connected (client_id=%d). Testing expiry %s", CLIENT_ID, expiry)

    # Find the underlying to pick a deep-OTM strike
    q = (await broker.get_quotes(["XSP"])).get("XSP")
    px = None
    if q:
        px = (
            (q.bid + q.ask) / 2.0
            if q.bid is not None and q.ask is not None
            else (q.last if q.last is not None else q.close)
        )
    if px is None:
        log.error("No XSP price available; using strike 600 blind")
        px = 600.0
    strike = float(round(px) - 30)  # 30 points OTM put: zero fill risk
    contract = OptionContract(symbol="XSP", expiry=expiry, strike=strike,
                              right=OptionRight.PUT, multiplier=100)
    log.info("Test contract: XSP %s %sP (underlying ~%.2f)", expiry, strike, px)

    def plan(price=0.01):
        return OrderPlan(
            order_intent_id=uuid4(), position_id=None, is_entry=True,
            strategy_id="cancel_shakeout", contract=contract,
            side=OrderSide.BUY, quantity=1, order_type="LMT",
            limit_price=price, order_ref=f"shakeout:{uuid4().hex[:8]}",
            timestamp=datetime.now(ZoneInfo("UTC")),
        )

    results = {}

    # --- Test 1+2: place -> confirm working -> cancel -> confirm cancelled
    o1 = await broker.place_order(plan())
    s = await wait_status(broker, o1.broker_order_id,
                          {OrderStatus.SUBMITTED}, 15.0)
    results["place_acknowledged"] = (s == OrderStatus.SUBMITTED, str(s))
    ok = await broker.cancel_order(o1.broker_order_id)
    s = await wait_status(broker, o1.broker_order_id,
                          {OrderStatus.CANCELLED, OrderStatus.REJECTED},
                          CANCEL_CONFIRM_TIMEOUT)
    results["cancel_confirmed"] = (
        ok and s == OrderStatus.CANCELLED, f"cancel_order={ok}, final={s}"
    )

    # --- Test 3: cancel/replace chain (repricer path)
    o2 = await broker.place_order(plan(0.01))
    await wait_status(broker, o2.broker_order_id, {OrderStatus.SUBMITTED}, 15.0)
    await broker.cancel_order(o2.broker_order_id)
    s2 = await wait_status(broker, o2.broker_order_id,
                           {OrderStatus.CANCELLED}, CANCEL_CONFIRM_TIMEOUT)
    o3 = await broker.place_order(plan(0.02))
    s3 = await wait_status(broker, o3.broker_order_id,
                           {OrderStatus.SUBMITTED}, 15.0)
    results["cancel_replace_chain"] = (
        s2 == OrderStatus.CANCELLED and s3 == OrderStatus.SUBMITTED,
        f"old={s2}, replacement={s3}",
    )
    await broker.cancel_order(o3.broker_order_id)
    await wait_status(broker, o3.broker_order_id,
                      {OrderStatus.CANCELLED}, CANCEL_CONFIRM_TIMEOUT)

    # --- Test 4: ground-truth status query (stuck-cancel sweep dependency)
    gt = await broker.get_order_status(o3.broker_order_id)
    results["ground_truth_query"] = (
        gt in (OrderStatus.CANCELLED, None), str(gt)
    )

    log.info("=" * 60)
    failed = 0
    for name, (passed, detail) in results.items():
        log.info("%-24s %s  (%s)", name, "PASS" if passed else "FAIL", detail)
        failed += 0 if passed else 1
    log.info("=" * 60)
    log.info("callbacks received: %d", len(statuses))

    await broker.disconnect()
    await store.stop()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
