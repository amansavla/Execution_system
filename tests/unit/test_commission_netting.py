from datetime import UTC, datetime
from uuid import uuid4

from src.app.runner import ExecutionRunner
from src.broker.ibkr_broker import IBKRBrokerClient
from src.core.enums import OptionRight, OrderSide, PositionStatus
from src.core.models import OptionContract, Position
from src.portfolio.position_manager import PositionManager
from src.storage.event_log import EventStore
from src.storage.position_store import PositionStore


# ---------------------------------------------------------------------------
# Broker side: _on_commission_report forwards (order_id, commission) once
# ---------------------------------------------------------------------------

class _EventStoreStub:
    def __init__(self):
        self.logged = []

    def log_callback(self, event_type, payload):
        self.logged.append((event_type, payload))


class _BrokerStub:
    def __init__(self):
        self._order_metadata = {}
        self._commission_callbacks = []
        self._seen_commission_exec_ids = set()
        self.event_store = _EventStoreStub()

    _on_commission_report = IBKRBrokerClient._on_commission_report


class _Exec:
    def __init__(self, order_id=101, exec_id="e1"):
        self.orderId = order_id
        self.execId = exec_id


class _Fill:
    def __init__(self, order_id=101, exec_id="e1"):
        self.execution = _Exec(order_id, exec_id)


class _Report:
    def __init__(self, commission=1.22, realized=None):
        self.commission = commission
        self.realizedPNL = realized


def test_commission_forwarded_once_and_deduped():
    broker = _BrokerStub()
    oid = uuid4()
    broker._order_metadata["101"] = {"order_id": oid, "strategy_id": "s1"}
    received = []
    broker._commission_callbacks.append(lambda o, c: received.append((o, c)))

    broker._on_commission_report(None, _Fill(101, "e1"), _Report(1.22))
    # duplicate report for the same execId (reconnect replay) must be ignored
    broker._on_commission_report(None, _Fill(101, "e1"), _Report(1.22))

    assert received == [(oid, 1.22)]
    assert [t for t, _ in broker.event_store.logged] == ["commission_report"]


def test_commission_for_unknown_order_or_zero_is_dropped():
    broker = _BrokerStub()
    received = []
    broker._commission_callbacks.append(lambda o, c: received.append((o, c)))

    broker._on_commission_report(None, _Fill(999, "e9"), _Report(1.22))  # unknown order
    broker._order_metadata["101"] = {"order_id": uuid4(), "strategy_id": "s1"}
    broker._on_commission_report(None, _Fill(101, "e2"), _Report(0.0))   # zero commission

    assert received == []


def test_ibkr_sentinel_realized_pnl_nulled():
    broker = _BrokerStub()
    broker._order_metadata["101"] = {"order_id": uuid4(), "strategy_id": "s1"}
    broker._on_commission_report(None, _Fill(101, "e3"), _Report(1.0, realized=1.7976931348623157e308))
    _, payload = broker.event_store.logged[0]
    assert payload["broker_realized_pnl"] is None


# ---------------------------------------------------------------------------
# Runner side: _on_commission nets into the matched position and persists
# ---------------------------------------------------------------------------

class _RunnerStub:
    _on_commission = ExecutionRunner._on_commission

    def __init__(self):
        self.position_manager = PositionManager(EventStore())
        self.position_store = PositionStore(":memory:")


def _pos(entry_order_id, exit_order_ids=(), pnl=100.0):
    return Position(
        position_id=uuid4(),
        strategy_id="xsp_straddle_manual",
        contract=OptionContract(symbol="XSP", expiry="20260713", strike=750.0,
                                 right=OptionRight.CALL, multiplier=100),
        side=OrderSide.SELL,
        quantity=5,
        average_entry_price=2.5,
        realized_pnl=pnl,
        status=PositionStatus.OPEN,
        entry_order_id=entry_order_id,
        exit_order_ids=list(exit_order_ids),
        entry_time=datetime.now(UTC),
    )


def test_commission_netted_on_entry_order_match():
    runner = _RunnerStub()
    entry_oid = uuid4()
    pos = _pos(entry_oid, pnl=100.0)
    runner.position_manager.positions[pos.position_id] = pos

    runner._on_commission(entry_oid, 3.05)
    assert pos.realized_pnl == 100.0 - 3.05
    assert pos.metadata["commission_paid"] == 3.05

    # second execution on the same order accumulates
    runner._on_commission(entry_oid, 1.0)
    assert pos.realized_pnl == 100.0 - 4.05
    assert pos.metadata["commission_paid"] == 4.05


def test_commission_netted_on_exit_order_match():
    runner = _RunnerStub()
    exit_oid = uuid4()
    pos = _pos(uuid4(), exit_order_ids=[exit_oid], pnl=50.0)
    runner.position_manager.positions[pos.position_id] = pos

    runner._on_commission(exit_oid, 2.0)
    assert pos.realized_pnl == 48.0


def test_unmatched_commission_is_logged_not_raised():
    runner = _RunnerStub()
    runner._on_commission(uuid4(), 1.22)  # no positions at all — must not raise
