"""Streamlit Operator Dashboard application entry point."""

from __future__ import annotations

import asyncio
from pathlib import Path
import streamlit as st

from src.broker.mock_broker import MockBrokerClient
from src.control.manual_control import ManualControlService
from src.control.overrides import OverrideManager
from src.core.config import BrokerConfig, load_overrides_config
from src.execution.order_manager import OrderManager
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore

# Set up page styling and layout
st.set_page_config(
    page_title="Operator Dashboard - US Option Strategy Execution System",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling (premium aesthetics)
st.markdown("""
<style>
    .main {
        background-color: #0e1117;
        color: #c9d1d9;
    }
    .stButton>button {
        border-radius: 4px;
        font-weight: bold;
    }
    .css-1kyx603 {
        background-color: #161b22;
    }
</style>
""", unsafe_allow_html=True)


async def load_system_state_async(db_path: str, overrides_path: Path) -> ManualControlService:
    """Helper to initialize EventStore and reconstruct state from SQLite logs."""
    # Ensure database path directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    event_store = EventStore(db_path)
    await event_store.start()

    # Reconstruct positions & orders by playing back events
    position_manager = PositionManager(event_store)
    
    # OrderManager requires broker client and event_store
    dummy_broker = MockBrokerClient()
    order_manager = OrderManager(dummy_broker, event_store)

    # Fetch and sort all events chronologically to reconstruct state
    if event_store._repo:
        all_events = await event_store._repo.get_all()
    else:
        all_events = []

    all_events = sorted(all_events, key=lambda e: e.timestamp)

    # Play back position fills and orders to populate managers
    for rec in all_events:
        if rec.event_type == "fill_event":
            from src.core.models import FillEvent
            try:
                fill = FillEvent.model_validate(rec.payload)
                position_manager.handle_fill(fill)
            except Exception:
                pass
        elif rec.event_type in ("order_event", "order_callback", "order_state_transition", "order_state_updated"):
            from src.core.models import OrderState
            try:
                order_state = OrderState.model_validate(rec.payload)
                order_manager.orders[order_state.order_id] = order_state
            except Exception:
                pass

    # Load OverrideManager
    if overrides_path.exists():
        try:
            config = load_overrides_config(overrides_path)
            override_manager = OverrideManager(config, persist_path=overrides_path)
        except Exception:
            override_manager = OverrideManager(persist_path=overrides_path)
    else:
        override_manager = OverrideManager(persist_path=overrides_path)

    # Instantiate central ManualControlService
    manual_control = ManualControlService(
        event_store=event_store,
        order_manager=order_manager,
        position_manager=position_manager,
        override_manager=override_manager,
        operator="dashboard_operator"
    )

    return manual_control


def load_manual_control_service() -> ManualControlService:
    """Synchronous interface to boot the service."""
    db_path = "data/events.db"
    overrides_path = Path("configs/overrides.yaml")
    
    # Run the async state loader synchronously
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        service = loop.run_until_complete(load_system_state_async(db_path, overrides_path))
        return service
    finally:
        loop.close()


def main() -> None:
    """Dashboard Main Layout & Navigation Routing."""
    st.sidebar.title("⚡ Execution Control")
    st.sidebar.subheader("US Option Strats")

    # Load manual control service
    manual_control = load_manual_control_service()

    # Route navigation
    menu = [
        "System Status",
        "Open Positions",
        "Active Orders",
        "Strategy Controls",
        "Risk Settings",
        "Execution Analytics",
        "Event Logs"
    ]
    choice = st.sidebar.radio("Navigation Menu", menu)

    # Import and render corresponding view
    from src.dashboard.views.status import render_status
    from src.dashboard.views.positions import render_positions
    from src.dashboard.views.orders import render_orders
    from src.dashboard.views.strategies import render_strategies
    from src.dashboard.views.risk import render_risk
    from src.dashboard.views.analytics import render_analytics
    from src.dashboard.views.logs import render_logs

    if choice == "System Status":
        render_status(manual_control)
    elif choice == "Open Positions":
        render_positions(manual_control)
    elif choice == "Active Orders":
        render_orders(manual_control)
    elif choice == "Strategy Controls":
        render_strategies(manual_control)
    elif choice == "Risk Settings":
        render_risk(manual_control)
    elif choice == "Execution Analytics":
        render_analytics(manual_control)
    elif choice == "Event Logs":
        render_logs(manual_control)

    # Shutdown the DB connection gracefully on streamlit script exit
    if hasattr(manual_control.event_store, "stop"):
        # We can stop EventStore background worker
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(manual_control.event_store.stop())
        except Exception:
            pass
        finally:
            loop.close()


if __name__ == "__main__":
    main()
