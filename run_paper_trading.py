#!/usr/bin/env python3
"""Launcher script to run the ExecutionRunner in Paper Trading mode connected to IBKR."""

import asyncio
import logging
from pathlib import Path
from datetime import datetime, UTC
from uuid import uuid4

from src.core.config import load_broker_config
from src.storage.event_log import EventStore
from src.broker.ibkr_broker import IBKRBrokerClient
from src.app.runner import ExecutionRunner, StrategyProvider
from src.core.models import StrategySignal, OptionContract
from src.core.enums import SignalDirection, OptionRight
from src.strategies import (
    XSPBreakoutStrategyProvider,
    XSPBreakoutLateStrategyProvider,
    XSPShortStraddleStrategyProvider,
    CompositeStrategyProvider,
)

# Configure logging.
# - INFO for our code; ib_async/aiosqlite wire-level chatter capped at WARNING
#   (DEBUG made runner.log grow to 570MB in one day, 94% ib_async.client).
# - Single rotating file sink. NO StreamHandler: the supervisor redirects
#   stdout into the same file, which double-wrote every line.
from logging.handlers import RotatingFileHandler

Path("data").mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    "data/runner.log", maxBytes=50 * 1024 * 1024, backupCount=5
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[_file_handler],
)
for noisy in ("ib_async.client", "ib_async.wrapper", "ib_async.ib", "aiosqlite"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("run_paper_trading")


async def main():
    # Ensure log/data directories exist
    Path("data").mkdir(parents=True, exist_ok=True)

    # 1. Load configuration
    configs_dir = Path("configs")
    broker_cfg_path = configs_dir / "broker.yaml"
    if not broker_cfg_path.exists():
        logger.error("Broker config file not found at %s", broker_cfg_path)
        return

    logger.info("Loading broker configuration...")
    broker_config = load_broker_config(broker_cfg_path)

    # Ensure we are in Paper mode as a safeguard
    if broker_config.live_trading.enabled:
        logger.error("Safety check failed: live_trading.enabled is true in configs/broker.yaml. Refusing to run from paper script.")
        return

    # 2. Start core services
    logger.info("Initializing EventStore database...")
    event_store = EventStore(db_path="data/events.db")

    logger.info("Initializing IBKR Broker Client...")
    broker_client = IBKRBrokerClient(broker_config, event_store)

    # 3. Instantiate Runner
    logger.info("Initializing ExecutionRunner with Composite Strategy Provider...")
    breakout_provider = XSPBreakoutStrategyProvider(broker=broker_client)
    breakout_late_provider = XSPBreakoutLateStrategyProvider(broker=broker_client)
    straddle_provider = XSPShortStraddleStrategyProvider(broker=broker_client)
    from src.strategies.dummy_test import DummyTestStrategyProvider
    dummy_provider = DummyTestStrategyProvider(broker=broker_client)
    from src.strategies.shakeout_cycle import ShakeoutCycleStrategyProvider
    shakeout_provider = ShakeoutCycleStrategyProvider(broker=broker_client)
    from src.strategies.xsp_5_ema import XSP5EMAStrategyProvider
    five_ema_provider = XSP5EMAStrategyProvider(broker=broker_client)

    strategy_provider = CompositeStrategyProvider({
        "xsp_breakout": breakout_provider,
        "xsp_breakout_late": breakout_late_provider,
        "xsp_short_straddle": straddle_provider,
        "dummy_test": dummy_provider,
        "shakeout_cycle": shakeout_provider,
        "xsp_5_ema": five_ema_provider,
    })
    
    runner = ExecutionRunner(
        broker=broker_client,
        event_store=event_store,
        strategy_provider=strategy_provider,
        configs_dir=configs_dir,
        tick_interval_seconds=1.0,           # Poll every 1 second
        reconciliation_interval_seconds=60.0  # Reconcile state every 60 seconds
    )

    # 4. Start execution
    try:
        logger.info("Starting ExecutionRunner...")
        await runner.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down...")
    except Exception as e:
        logger.error("Fatal error in execution loop: %s", e, exc_info=True)
    finally:
        logger.info("Stopping ExecutionRunner...")
        await runner.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
