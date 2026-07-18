"""Defect regression: while root hangs on a subtask barrier the session must
not end the turn.

Under foreground fan-out (spawn_subagent with multiple entries), the root
task goes TaskSuspended after SubtaskSpawned with
wake_on=SubtaskGroupCompleted. Before the fix, _update_status could not read
a ``handle`` (that condition has no handle field) → wrongly set idle + the
translator emitted turn_finished, producing a fake completion ("subagent
still executing, yet the session shows as ready for input"). After the fix
this suspension stays running and emits no turn_finished (for the translator
side see test_translator.test_lifecycle_mapping).

Tested directly against AgentService._update_status (no startup, no noeta /
sandbox connection).
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from noeta.agent.config import Settings
from noeta.agent.host.service import AgentService
from noeta.agent.store.sessions import SessionStore


@pytest.fixture
def svc_env(tmp_path):
    settings = Settings(
        llm_provider="mock",
        data_dir=str(tmp_path / "data"),
        shared_data_dir=str(tmp_path / "shared"),
    )
    store = SessionStore(tmp_path / "app.db")
    service = AgentService(settings, store)
    # Title generation calls the LLM on a separate thread and is irrelevant
    # here; null it out to avoid thread side effects.
    service._maybe_generate_title = lambda *a, **k: None  # type: ignore[method-assign]
    yield store, service
    store.close()


def _suspended(wake_on: NS, reason: str) -> NS:
    return NS(type="TaskSuspended", payload=NS(wake_on=wake_on, reason=reason))


def test_subtask_barrier_keeps_running(svc_env):
    """While root waits on the subtask barrier the session stays running, not
    idle."""
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    store.update(sid, status="running")

    wake = NS(
        __canonical_tag__="subtask_group_completed",
        group_id="g-1",
        subtask_ids=("t1", "t2"),
        concurrent=True,
    )
    service._update_status(_suspended(wake, "waiting_subtask_group"), sid, "task-x")
    assert store.get(sid).status == "running"


def test_subtask_barrier_single_subtask_keeps_running(svc_env):
    """A single-subtask SubtaskCompleted condition likewise stays running."""
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    store.update(sid, status="running")

    wake = NS(
        __canonical_tag__="subtask_completed", subtask_id="t1", result=None
    )
    service._update_status(_suspended(wake, "waiting_subtask"), sid, "task-x")
    assert store.get(sid).status == "running"


def test_next_goal_still_idle(svc_env):
    """Regression: a next-goal suspension (waiting for the user's next turn)
    still sets idle, unaffected by the subtask short-circuit."""
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    store.update(sid, status="running")

    wake = NS(handle="noeta-code-next-goal")
    service._update_status(_suspended(wake, "waiting_human"), sid, "task-x")
    assert store.get(sid).status == "idle"


def test_question_still_waiting(svc_env):
    """Regression: a question suspension still sets waiting."""
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    store.update(sid, status="running")

    wake = NS(handle="question-c1")
    service._update_status(_suspended(wake, "waiting_human"), sid, "task-x")
    assert store.get(sid).status == "waiting"
