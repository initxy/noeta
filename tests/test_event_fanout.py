"""EventFanout unit tests (issue 23).

Tests the Observer → Broadcaster glue. Uses a fake broadcaster so the
test stays pure to the L2 boundary (no HTTP).
"""

from __future__ import annotations

from noeta.observers.fanout import EventFanout
from noeta.protocols.events import EventEnvelope
from noeta.storage.memory import InMemoryEventLog


def _env(idx: int) -> EventEnvelope:
    return EventEnvelope(
        id=f"env-{idx}",
        task_id="t-1",
        seq=idx,
        type="TaskCreated",
        schema_version=1,
        occurred_at=0.0,
        actor="test",
        trace_id="trace",
        correlation_id="corr",
        causation_id=None,
        payload={"i": idx},
        origin="engine",
    )


class _FakeBroadcaster:
    def __init__(self) -> None:
        self.published: list[EventEnvelope] = []

    def publish(self, env: EventEnvelope) -> None:
        self.published.append(env)


def test_fanout_forwards_envelope_to_broadcaster() -> None:
    log = InMemoryEventLog()
    bc = _FakeBroadcaster()
    obs = EventFanout(event_log=log, broadcaster=bc)  # type: ignore[arg-type]
    env = log.system_emit(
        task_id="t-1",
        type="TaskCreated",
        payload={"x": 1},
        actor="engine",
        origin="engine",
    )
    assert bc.published == [env]
    obs.stop()


def test_fanout_stop_unsubscribes() -> None:
    log = InMemoryEventLog()
    bc = _FakeBroadcaster()
    obs = EventFanout(event_log=log, broadcaster=bc)  # type: ignore[arg-type]
    obs.stop()
    log.system_emit(
        task_id="t-1",
        type="TaskCreated",
        payload={"x": 1},
        actor="engine",
        origin="engine",
    )
    # After stop, no more envelopes reach the broadcaster
    assert bc.published == []


def test_fanout_stop_is_idempotent() -> None:
    log = InMemoryEventLog()
    bc = _FakeBroadcaster()
    obs = EventFanout(event_log=log, broadcaster=bc)  # type: ignore[arg-type]
    obs.stop()
    obs.stop()  # must not raise


def test_fanout_swallows_broadcaster_exceptions() -> None:
    """Observer callbacks fire outside the EventLog writer lock but must
    not raise back to the writer. If the broadcaster blows up, the
    observer logs and moves on."""

    class _BlowingBroadcaster:
        def __init__(self) -> None:
            self.calls = 0

        def publish(self, env: EventEnvelope) -> None:
            self.calls += 1
            raise RuntimeError("boom")

    log = InMemoryEventLog()
    bc = _BlowingBroadcaster()
    EventFanout(event_log=log, broadcaster=bc)  # type: ignore[arg-type]
    # Must not raise even though broadcaster.publish blows up
    log.system_emit(
        task_id="t-1",
        type="TaskCreated",
        payload={"x": 1},
        actor="engine",
        origin="engine",
    )
    assert bc.calls == 1
