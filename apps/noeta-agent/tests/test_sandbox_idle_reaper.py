"""Sandbox idle-reclaim reaper (AgentService._reap_idle_sandboxes).

Criteria: session.status == 'idle' and now - updated_at beyond each tier's
threshold (hours). waiting (awaiting a follow-up) / running (subtask barrier)
are not reclaimed; sessions without a task never started a container and are
not scanned. Two tiers:
- stop (short TTL) → provider.stop_idle(session_id): stop the container but
  keep the body; continuing the conversation attaches and docker start brings
  it back (continuation goes resume→attach and never re-allocates, so this
  tier must not tear the container down).
- remove (long TTL) → provider.force_release(session_id): really tear down,
  reclaiming disk.

Drives _reap_idle_sandboxes directly (no reaper thread, no interval wait),
with a fake provider recording each tier's actions.
"""
from __future__ import annotations

import time

import pytest

from noeta.agent.host.service import AgentService
from noeta.agent.config import Settings
from noeta.agent.store.sessions import SessionStore


class _FakeProvider:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.released: list[str] = []

    def stop_idle(self, session_id: str) -> bool:
        self.stopped.append(session_id)
        return True

    def force_release(self, session_id: str) -> None:
        self.released.append(session_id)


@pytest.fixture
def svc(tmp_path):
    settings = Settings(
        llm_provider="mock",
        data_dir=str(tmp_path / "data"),
        shared_data_dir=str(tmp_path / "shared"),
        sandbox_enabled=True,
        sandbox_idle_stop_hours=1.0,
        sandbox_idle_remove_hours=24.0,
    )
    store = SessionStore(tmp_path / "app.db")
    service = AgentService(settings, store)
    service._sandbox_provider = _FakeProvider()
    yield settings, store, service
    store.close()


def _reap(service, settings) -> None:
    service._reap_idle_sandboxes(
        settings.sandbox_idle_stop_hours * 3600.0,
        settings.sandbox_idle_remove_hours * 3600.0,
    )


def _make_session(store, *, status, hours_ago, task_id="task-1"):
    """Create an idle/running/waiting session and dial updated_at back N
    hours."""
    s = store.create(user="alice", model="m", space_id="sp1")
    updated = time.time() - hours_ago * 3600.0
    with store._lock:
        store._conn.execute(
            "UPDATE sessions SET status=?, updated_at=?, task_id=? WHERE id=?",
            (status, updated, task_id, s.id),
        )
    return s.id


def test_stops_idle_past_stop_tier(svc):
    """idle beyond the stop tier → stop the container (not tear down:
    continuation needs attach to start it back)."""
    settings, store, service = svc
    sid = _make_session(store, status="idle", hours_ago=2.0)
    _reap(service, settings)
    assert service._sandbox_provider.stopped == [sid]
    assert service._sandbox_provider.released == []


def test_removes_idle_past_remove_tier(svc):
    """idle beyond the remove tier → really tear down (reclaim disk), no
    longer just stop."""
    settings, store, service = svc
    sid = _make_session(store, status="idle", hours_ago=30.0)
    _reap(service, settings)
    assert service._sandbox_provider.released == [sid]
    assert service._sandbox_provider.stopped == []


def test_keeps_recent_idle_session(svc):
    """idle but under the stop tier → untouched."""
    settings, store, service = svc
    _make_session(store, status="idle", hours_ago=0.5)
    _reap(service, settings)
    assert service._sandbox_provider.stopped == []
    assert service._sandbox_provider.released == []


def test_keeps_waiting_and_running(svc):
    """waiting (awaiting a follow-up) / running (subtask barrier) are never
    reclaimed, however overdue."""
    settings, store, service = svc
    _make_session(store, status="waiting", hours_ago=5.0)
    _make_session(store, status="running", hours_ago=30.0, task_id="task-2")
    _reap(service, settings)
    assert service._sandbox_provider.stopped == []
    assert service._sandbox_provider.released == []


def test_ignores_session_without_task(svc):
    """A session without a task never started a container;
    list_all_with_task does not return it, no reclamation."""
    settings, store, service = svc
    # create defaults task_id=None; don't dial task_id
    s = store.create(user="alice", model="m", space_id="sp1")
    with store._lock:
        store._conn.execute(
            "UPDATE sessions SET status='idle', updated_at=? WHERE id=?",
            (time.time() - 30 * 3600.0, s.id),
        )
    _reap(service, settings)
    assert service._sandbox_provider.stopped == []
    assert service._sandbox_provider.released == []


def test_each_tier_hits_its_own_sessions(svc):
    """Mixed batch: each session lands in its tier by idle duration, the rest
    untouched."""
    settings, store, service = svc
    to_stop = _make_session(store, status="idle", hours_ago=3.0)
    to_remove = _make_session(store, status="idle", hours_ago=48.0, task_id="task-2")
    _make_session(store, status="idle", hours_ago=0.2, task_id="task-3")
    _make_session(store, status="running", hours_ago=48.0, task_id="task-4")
    _reap(service, settings)
    assert service._sandbox_provider.stopped == [to_stop]
    assert service._sandbox_provider.released == [to_remove]


def test_stop_tier_disabled(svc):
    """stop tier disabled → only the long-TTL removal remains; short idle
    untouched."""
    settings, store, service = svc
    _make_session(store, status="idle", hours_ago=3.0)
    old = _make_session(store, status="idle", hours_ago=48.0, task_id="task-2")
    service._reap_idle_sandboxes(0.0, 24.0 * 3600.0)
    assert service._sandbox_provider.stopped == []
    assert service._sandbox_provider.released == [old]


def test_remove_tier_disabled_keeps_stopping(svc):
    """remove tier disabled → however old, only stop, never tear down (the
    container is always recoverable)."""
    settings, store, service = svc
    sid = _make_session(store, status="idle", hours_ago=999.0)
    service._reap_idle_sandboxes(1.0 * 3600.0, 0.0)
    assert service._sandbox_provider.stopped == [sid]
    assert service._sandbox_provider.released == []


def test_no_provider_is_noop(svc):
    """Calling with no provider assembled (sandbox disabled) is safe and
    side-effect free."""
    settings, store, service = svc
    _make_session(store, status="idle", hours_ago=5.0)
    service._sandbox_provider = None
    _reap(service, settings)
    # passing without raising is the assertion


def test_already_stopped_is_not_logged_twice(svc):
    """stop_idle returning False (was not running anyway) does not count as a
    reclamation — and does not affect later sessions."""
    settings, store, service = svc

    class _AlreadyStopped(_FakeProvider):
        def stop_idle(self, session_id: str) -> bool:
            self.stopped.append(session_id)
            return False

    sid = _make_session(store, status="idle", hours_ago=2.0)
    service._sandbox_provider = _AlreadyStopped()
    _reap(service, settings)
    assert service._sandbox_provider.stopped == [sid]


def test_reap_exception_does_not_break(svc):
    """One session's reclamation raising does not block the others."""
    settings, store, service = svc

    class _FlakyProvider(_FakeProvider):
        def stop_idle(self, session_id: str) -> bool:
            if session_id == boom:
                raise RuntimeError("boom")
            return super().stop_idle(session_id)

    boom = _make_session(store, status="idle", hours_ago=2.0)
    ok = _make_session(store, status="idle", hours_ago=2.0, task_id="task-2")
    service._sandbox_provider = _FlakyProvider()
    _reap(service, settings)
    assert service._sandbox_provider.stopped == [ok]
