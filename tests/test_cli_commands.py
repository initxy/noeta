"""Runtime-assembly + resume seam tests (originally issue 23).

These tests used to drive the operator CLI argparse path
(``noeta run`` / ``inspect`` / ``resume``).
The library-SDK refactor removed the operator command suite; the meaningful
behaviour they covered now lives behind cli-free library seams:

* runtime assembly — :func:`noeta.testing.profile.build_runtime`,
* the resume 3-state machine — :func:`noeta.runtime.worker.run_leased_task`
  (drain).
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    Usage,
)
from noeta.runtime.worker import run_leased_task
from noeta.testing.profile import (
    build_runtime,
    build_sqlite_stack,
    build_tools,
    default_budget,
    default_permission_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._responses:
            raise RuntimeError("scripted exhausted")
        self.calls += 1
        return self._responses.pop(0)


SYSTEM_PROMPT = "You are a helpful assistant."


def _drain(bundle: Any) -> dict[str, int]:
    """Drive the dispatcher's ready queue empty via the shared
    :func:`run_leased_task` 3-state machine (the cli-free seam the old
    ``noeta resume`` drain loop wrapped). Returns the same counters the
    operator command used to print."""
    drained = woken = skipped = 0
    while True:
        lease = bundle.dispatcher.lease(
            worker_id="drain", lease_seconds=600.0, task_id=None
        )
        if lease is None:
            break
        outcome = run_leased_task(bundle, lease)
        if outcome == "woken":
            woken += 1
        elif outcome == "drained":
            drained += 1
        else:  # "skipped"
            skipped += 1
    return {"drained": drained, "woken": woken, "skipped_suspended": skipped}


# ---------------------------------------------------------------------------
# resume drain (no-target path)
# ---------------------------------------------------------------------------


def test_resume_empty_dispatcher_drains_nothing() -> None:
    """Drain over an empty in-memory stack: no tasks → all counters
    zero. This is the cli-free shape the old ``noeta resume`` (no
    ``--task-id``) emitted as ``{ok, drained: 0, woken: 0, ...}``."""
    bundle = build_runtime(
        provider=_ScriptedProvider([]),
        model="test-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=None,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        counters = _drain(bundle)
    finally:
        bundle.shutdown()
    assert counters["drained"] == 0
    assert counters["woken"] == 0
    assert counters["skipped_suspended"] == 0


def _seed_pending_task_in_sqlite(db_path: str, goal: str = "drain me") -> str:
    """Build a sqlite-backed bundle, create a task, enqueue it but do
    not lease/run. Returns the task_id so the second-phase ``resume``
    test can assert it."""
    bundle = build_runtime(
        provider=_ScriptedProvider([]),
        model="test-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=db_path,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        task = bundle.engine.create_task(goal=goal, policy_name="react")
        bundle.dispatcher.enqueue(task.task_id)
        return task.task_id
    finally:
        bundle.shutdown()


def test_resume_drains_pending_task_via_sqlite(tmp_path) -> None:
    """rev3 B1 / G6: a pending Task left in the dispatcher gets
    drained (lease → fold → run_one_step → release). The fake
    provider is reached because the task is not suspended."""
    db_path = str(tmp_path / "resume_pending.sqlite")
    _seed_pending_task_in_sqlite(db_path)

    fake_provider = _ScriptedProvider(
        [
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )

    bundle = build_runtime(
        provider=fake_provider,
        model="stub",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=db_path,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        counters = _drain(bundle)
    finally:
        bundle.shutdown()
    assert counters["drained"] == 1
    assert counters["skipped_suspended"] == 0
    # Provider was reached for the pending task (LLM was called)
    assert fake_provider.calls == 1


def _seed_suspended_task_in_sqlite(db_path: str) -> tuple[str, Any]:
    """Drive a task through a SpawnSubtaskDecision so it suspends with
    a non-trivial ``wake_on``, then leave the parent in the dispatcher's
    suspended state. Returns ``(task_id, wake_on)``."""
    from noeta.guards.permission import PermissionPolicy
    from noeta.policies.stub import StubScriptedPolicy
    from noeta.protocols.decisions import SpawnSubtaskDecision

    perm = PermissionPolicy(
        allowed_tools=frozenset({"echo"}),
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=frozenset({"helper"}),
    )
    bundle = build_runtime(
        provider=_ScriptedProvider([]),
        model="test-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=db_path,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=perm,
        budget=default_budget(),
    )
    try:
        # Spawn a subtask → parent suspends waiting_subtask
        bundle.engine._policy = StubScriptedPolicy(  # type: ignore[attr-defined]
            [SpawnSubtaskDecision(agent_name="helper", goal="sub-job")]
        )
        task = bundle.engine.create_task(goal="parent", policy_name="stub")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(
            worker_id="seed", lease_seconds=60.0
        )
        assert lease is not None
        result = bundle.engine.run_one_step(task, lease_id=lease.lease_id)
        bundle.dispatcher.release(
            lease.lease_id,
            next_state=result.status,
            wake_on=result.wake_on,
        )
        assert result.status == "suspended"
        return task.task_id, result.wake_on
    finally:
        bundle.shutdown()


def test_resume_skips_suspended_task_without_wake_event(tmp_path) -> None:
    """At-most-once-loss recovery seam: when the dispatcher hands out a
    lease on a task that fold says is suspended **but** does not deliver
    a ``wake_event`` (the wake was lost between a prior lease and crash),
    the resume machine must skip + release(suspended) with ``wake_on``
    preserved so a future wake delivery can still recover it.

    We simulate the lost-wake state by enqueueing the suspended task
    directly (skipping the wake() handshake that would normally set
    ``matched_wake_event_canonical``). lease() therefore returns
    wake_event=None.
    """
    db_path = str(tmp_path / "resume_suspended.sqlite")
    parent_task_id, original_wake_on = _seed_suspended_task_in_sqlite(db_path)
    assert original_wake_on is not None

    _, _, seed_dispatcher = build_sqlite_stack(db_path)
    # Force the task back onto the ready queue WITHOUT going through
    # wake() — this is the at-most-once-loss shape (e.g. a previous
    # lease consumed the matched_wake_event, then the worker crashed
    # and requeue_stale brought the row back to ready with the column
    # cleared).
    seed_dispatcher.enqueue(parent_task_id)

    fake_provider = _ScriptedProvider([])  # raise if called

    bundle = build_runtime(
        provider=fake_provider,
        model="stub",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=db_path,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        counters = _drain(bundle)
    finally:
        bundle.shutdown()
    assert counters["skipped_suspended"] >= 1
    assert counters["woken"] == 0
    # No LLM call — the skip path must not reach the provider.
    assert fake_provider.calls == 0

    # wake_on preservation: a fresh dispatcher on the same sqlite file
    # can still wake the parent with the original canonical wake_on.
    # If the skip path had cleared wake_on, this wake() would return False.
    _, _, verify_dispatcher = build_sqlite_stack(db_path)
    assert verify_dispatcher.wake(parent_task_id, original_wake_on) is True, (
        "resume must release suspended tasks with wake_on preserved; "
        "wake() returned False which means resume cleared it"
    )


def test_resume_wakes_suspended_task_when_lease_carries_wake_event(
    tmp_path,
) -> None:
    """Wake-resume happy path + ``TaskWoken.wake_event`` payload
    assertion (B2): a suspended parent is woken via
    ``dispatcher.wake(...)`` with a result-populated
    ``SubtaskCompleted`` event (projection-matching still succeeds
    because matching projects on ``subtask_id`` only). The resume
    machine leases the parent with ``Lease.wake_event`` populated and
    threads it into ``engine.note_woken`` before ``run_one_step`` — the
    durable ``TaskWoken`` envelope must carry the same
    ``SubtaskCompleted(subtask_id=X, result=R)`` payload back into the
    EventLog.
    """
    from noeta.protocols.wake import SubtaskCompleted, SubtaskResult

    db_path = str(tmp_path / "resume_wake.sqlite")
    parent_task_id, original_wake_on = _seed_suspended_task_in_sqlite(db_path)
    assert original_wake_on is not None
    assert isinstance(original_wake_on, SubtaskCompleted)

    _, _, seed_dispatcher = build_sqlite_stack(db_path)
    # Wake with a result-populated event. Projection matching matches
    # by ``subtask_id`` only (result is informational); the wake_event
    # carries the result forward into the lease handoff.
    expected_result = SubtaskResult(
        status="completed", output="round-trip-payload"
    )
    wake_event_with_result = SubtaskCompleted(
        subtask_id=original_wake_on.subtask_id, result=expected_result
    )
    assert seed_dispatcher.wake(parent_task_id, wake_event_with_result) is True

    fake_provider = _ScriptedProvider([])

    bundle = build_runtime(
        provider=fake_provider,
        model="stub",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=db_path,
        sse_broadcaster=None,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        counters = _drain(bundle)
    finally:
        bundle.shutdown()
    assert counters["woken"] >= 1
    assert counters["skipped_suspended"] == 0
    # The lease delivered a wake_event, so the run did not touch the
    # provider (the stub policy fires the run-one-step branches before
    # any LLM call).
    assert fake_provider.calls == 0

    # Durable TaskWoken envelope landed in the EventLog with the exact
    # wake_event payload (projection-matching preserves result round-trip).
    read_log, read_cs, _ = build_sqlite_stack(db_path)
    envelopes = read_log.read(parent_task_id)
    woken_envs = [e for e in envelopes if e.type == "TaskWoken"]
    assert len(woken_envs) >= 1, (
        "wake-resume must write a TaskWoken envelope when "
        "Lease.wake_event is non-None"
    )
    woken_payload = woken_envs[0].payload
    assert isinstance(woken_payload.wake_event, SubtaskCompleted)
    assert woken_payload.wake_event.subtask_id == original_wake_on.subtask_id
    assert woken_payload.wake_event.result == expected_result


# ---------------------------------------------------------------------------
# run (runtime assembly drives a task to terminal)
# ---------------------------------------------------------------------------


def test_run_creates_task_with_fake_provider() -> None:
    """End-to-end runtime assembly: a fake LLM provider drives a task
    created via :func:`build_runtime` to a terminal status (the seam the
    old ``noeta run`` wrapped)."""
    fake_provider = _ScriptedProvider(
        [
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )

    bundle = build_runtime(
        provider=fake_provider,
        model="stub-model",
        system_prompt=SYSTEM_PROMPT,
        tools=build_tools(),
        sqlite_path=None,
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    try:
        task = bundle.engine.create_task(goal="say done", policy_name="react")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(worker_id="run", lease_seconds=60.0)
        assert lease is not None
        bundle.engine.append_user_message(
            task, content=[TextBlock(text="say done")], lease_id=lease.lease_id
        )
        task = bundle.engine.run_one_step(task, lease_id=lease.lease_id)
        bundle.dispatcher.release(lease.lease_id, next_state=task.status)
        events = bundle.event_log.read(task.task_id)
    finally:
        bundle.shutdown()
    assert task.status == "terminal"
    assert len(events) >= 3
    # Provider was actually invoked
    assert fake_provider.calls == 1
