"""Defect regression: late events arriving after a terminal state must not
resurrect the session status.

`_on_envelope` runs on the **emit thread**, and cancel and the normal turn
emit from two different threads:

- `driver.cancel()` calls `system_emit(TaskCancelled)` directly on the
  **request thread**, without waiting for a step boundary;
- that turn's `TaskSuspended` is emitted by the **worker thread** at its own
  pace.

There is no ordering guarantee between their writes to `session.status`. On
top of that, `UserQuestionRequested` sets the status to waiting **early** (so
the client can answer as soon as it receives the question), so the client can
see waiting and issue a cancel before `TaskSuspended` has been emitted, hence:

    worker:  UserQuestionRequested → waiting
    client:  sees waiting → POST /cancel
    request: system_emit(TaskCancelled) → idle       ← turn terminated
    client:  sees idle → POST /messages
    worker:  TaskSuspended(question-*) → waiting     ← late, resurrects the terminal state
    request: send_message sees waiting → 409

The symptom was test_api_flow.test_cancel_then_continue flaking with
409 != 202: after a cancel the session stayed stuck in waiting forever — the
user could not send a new message, and there was no real question to answer
either.

Criterion: a terminal state (TaskCancelled/TaskFailed/TaskCompleted) is an
**absorbing state** for that task — no later event of that task changes the
status again. Tested directly against AgentService._update_status (no
startup, no noeta / sandbox connection), because the thread interleaving can
only be reproduced by lottery in an integration test.
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
    # Title generation / memory consolidation run on separate threads and are
    # irrelevant here; null them out to avoid thread side effects.
    service._maybe_generate_title = lambda *a, **k: None  # type: ignore[method-assign]
    service._maybe_consolidate_memory = lambda *a, **k: None  # type: ignore[method-assign]
    yield store, service
    store.close()


def _question() -> NS:
    return NS(type="UserQuestionRequested", payload=NS())


def _suspended_on_question() -> NS:
    return NS(
        type="TaskSuspended",
        payload=NS(wake_on=NS(handle="question-abc"), reason="ask"),
    )


def _cancelled() -> NS:
    return NS(type="TaskCancelled", payload=NS(reason="user_cancelled"))


def test_late_suspend_after_cancel_does_not_resurrect_waiting(svc_env):
    """Core regression: a TaskSuspended arriving after TaskCancelled must not
    flip the status back to waiting.

    Before the fix this ended up waiting, and the user's new message was
    judged busy by send_message → 409.
    """
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    tid = "task-1"
    store.update(sid, status="running")

    service._update_status(_question(), sid, tid)
    assert store.get(sid).status == "waiting"  # the early waiting

    service._update_status(_cancelled(), sid, tid)
    assert store.get(sid).status == "idle"  # turn terminated

    # The worker thread's TaskSuspended arrives late (cancel took the request
    # thread's shortcut).
    service._update_status(_suspended_on_question(), sid, tid)
    assert store.get(sid).status == "idle"


def test_late_events_after_cancel_do_not_resurrect_running(svc_env):
    """Likewise: late TaskStarted/TaskWoken must not flip a cancelled turn
    back to running."""
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id
    tid = "task-1"

    service._update_status(_cancelled(), sid, tid)
    assert store.get(sid).status == "idle"

    for etype in ("TaskStarted", "TaskWoken"):
        service._update_status(NS(type=etype, payload=NS()), sid, tid)
        assert store.get(sid).status == "idle", etype


def test_terminal_is_per_task_not_global(svc_env):
    """The absorbing state is tracked per task: cancelling an old task must
    not freeze the status of a new task in the same session.

    This is exactly the second half of test_cancel_then_continue — continuing
    the conversation after a cancel starts a new task (the old task is
    NotResumable), and it must be able to set the session back to running.
    """
    store, service = svc_env
    sid = store.create("u", "m", "space-1").id

    service._update_status(_cancelled(), sid, "task-old")
    assert store.get(sid).status == "idle"

    service._update_status(NS(type="TaskStarted", payload=NS()), sid, "task-new")
    assert store.get(sid).status == "running"
