"""Fold-side governance accumulation tests (issue 18).

The Engine relies on fold's handlers to keep ``GovernanceState`` in
sync with the EventLog so BudgetGuard reads accurate counters via the
``_guard`` refold. Each handler is tested in isolation here; cross-
adapter wiring is covered in ``test_budget_guard_engine_integration``.
"""

from __future__ import annotations

from noeta.core.fold import fold
from noeta.protocols.events import (
    BackgroundShellExitedPayload,
    BackgroundShellKilledPayload,
    BackgroundShellPolledPayload,
    BackgroundShellStartedPayload,
    ContextPlanComposedPayload,
    LLMRequestFinishedPayload,
    SubtaskCompletedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCancelledPayload,
    TaskCreatedPayload,
    ToolCallDeniedPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.messages import Usage
from noeta.protocols.values import ContentRef
from noeta.protocols.wake import SubtaskResult
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def _make_runtime():
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    log.emit(
        task_id="t1",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    return log, cs


def _emit_plan(log, n):
    ref = ContentRef(hash="x" * 64, size=4, media_type="application/json")
    for _ in range(n):
        log.emit(
            task_id="t1",
            type="ContextPlanComposed",
            payload=ContextPlanComposedPayload(plan_ref=ref),
        )


def test_iterations_accumulate_from_context_plan_composed() -> None:
    log, cs = _make_runtime()
    _emit_plan(log, 3)
    assert fold(log, cs, "t1").governance.iterations == 3


def test_tool_calls_accumulate_from_tool_call_started() -> None:
    log, cs = _make_runtime()
    for i in range(4):
        log.emit(
            task_id="t1",
            type="ToolCallStarted",
            payload=ToolCallStartedPayload(
                call_id=f"c{i}", tool_name="echo", arguments={}
            ),
        )
    assert fold(log, cs, "t1").governance.tool_calls == 4


def test_spawned_subtasks_accumulate_from_subtask_spawned() -> None:
    log, cs = _make_runtime()
    for i in range(2):
        log.emit(
            task_id="t1",
            type="SubtaskSpawned",
            payload=SubtaskSpawnedPayload(
                subtask_id=f"c{i}", agent_name="child", goal="g"
            ),
        )
    assert fold(log, cs, "t1").governance.spawned_subtasks == 2


def test_cost_usd_accumulates_from_llm_request_finished() -> None:
    log, cs = _make_runtime()
    for cost in (0.05, 0.1, 0.0):  # zero contributes nothing
        log.emit(
            task_id="t1",
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id="L", success=True, cost_usd=cost
            ),
        )
    assert abs(fold(log, cs, "t1").governance.cost_usd - 0.15) < 1e-9


def test_tokens_accumulate_from_llm_request_finished_usage() -> None:
    """Foundation A: fold accumulates per-token counters from the typed Usage.

    ``input_tokens`` is the derived uncached+cache_read+cache_write total,
    kept distinct from the cache breakdown so ① can price them separately.
    """
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="LLMRequestFinished",
        payload=LLMRequestFinishedPayload(
            call_id="L1",
            success=True,
            usage=Usage(
                uncached=10,
                cache_read=5,
                cache_write=2,
                output=20,
                reasoning_tokens=3,
            ),
        ),
    )
    g = fold(log, cs, "t1").governance
    assert g.input_tokens == 17  # 10 + 5 + 2
    assert g.output_tokens == 20
    assert g.cache_read_tokens == 5
    assert g.cache_write_tokens == 2
    assert g.reasoning_tokens == 3


def test_last_input_tokens_is_latest_turn_not_accumulated() -> None:
    """``RuntimeState.last_input_tokens`` is the LAST turn's real
    input total (last-write-wins), NOT the running accumulator the governance
    counters keep. Three turns of input 5/10/7 → governance accumulates 22 but
    runtime.last_input_tokens is 7 (the final turn)."""
    log, cs = _make_runtime()
    for u in (5, 10, 7):
        log.emit(
            task_id="t1",
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id="L",
                success=True,
                usage=Usage(uncached=u, output=1),
            ),
        )
    task = fold(log, cs, "t1")
    assert task.runtime.last_input_tokens == 7   # latest turn only
    assert task.governance.input_tokens == 22    # 5 + 10 + 7 (accumulated)


def test_last_input_tokens_includes_cache_breakdown() -> None:
    """``last_input_tokens`` mirrors ``Usage.input`` = uncached+cache_read+
    cache_write — the precise size the whole prompt cost last time, the thing
    the chars/4 heuristic systematically under-counts."""
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="LLMRequestFinished",
        payload=LLMRequestFinishedPayload(
            call_id="L",
            success=True,
            usage=Usage(uncached=100, cache_read=900, cache_write=50, output=20),
        ),
    )
    assert fold(log, cs, "t1").runtime.last_input_tokens == 1050  # 100+900+50


def test_last_input_tokens_zero_without_usage() -> None:
    """Byte-safe: an old recording's LLMRequestFinished has no ``usage`` field
    → ``last_input_tokens`` stays at its default 0 (the trigger then falls back
    to a pure estimate)."""
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="LLMRequestFinished",
        payload=LLMRequestFinishedPayload(call_id="L", success=True),
    )
    env = log.read("t1")[-1]
    object.__setattr__(env.payload, "usage", None)
    assert fold(log, cs, "t1").runtime.last_input_tokens == 0


def test_tokens_sum_across_multiple_llm_request_finished() -> None:
    log, cs = _make_runtime()
    for _ in range(3):
        log.emit(
            task_id="t1",
            type="LLMRequestFinished",
            payload=LLMRequestFinishedPayload(
                call_id="L",
                success=True,
                usage=Usage(uncached=4, cache_read=1, output=8),
            ),
        )
    g = fold(log, cs, "t1").governance
    assert g.input_tokens == 15  # (4 + 1) * 3
    assert g.output_tokens == 24
    assert g.cache_read_tokens == 3


def test_old_recording_without_usage_keeps_token_counters_zero() -> None:
    """Byte-safe: an old recording's payload predates the ``usage`` field.

    We simulate the old shape by stripping ``usage`` off the folded payload
    so fold sees a payload object with no such attribute — the getattr
    fallback must leave every token counter at its default 0, matching the
    from-scratch fold of a pre-Foundation-A stream.
    """
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="LLMRequestFinished",
        payload=LLMRequestFinishedPayload(call_id="L", success=True),
    )
    # Drop the usage attribute to mimic a payload restored from a recording
    # that never had the field (the fold getattr-tolerance contract).
    env = log.read("t1")[-1]
    object.__setattr__(env.payload, "usage", None)
    g = fold(log, cs, "t1").governance
    assert g.input_tokens == 0
    assert g.output_tokens == 0
    assert g.cache_read_tokens == 0
    assert g.cache_write_tokens == 0
    assert g.reasoning_tokens == 0


def test_denied_collects_tool_call_denied_records() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="ToolCallDenied",
        payload=ToolCallDeniedPayload(
            call_id="c1", tool_name="bad", reason="policy"
        ),
    )
    denied = fold(log, cs, "t1").governance.denied
    assert len(denied) == 1
    assert denied[0]["type"] == "ToolCallDenied"
    assert denied[0]["tool_name"] == "bad"
    assert denied[0]["reason"] == "policy"


def test_denied_collects_subtask_denied_records() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="SubtaskDenied",
        payload=SubtaskDeniedPayload(
            agent_name="x", goal="g", reason="not-allowed"
        ),
    )
    denied = fold(log, cs, "t1").governance.denied
    assert len(denied) == 1
    assert denied[0]["type"] == "SubtaskDenied"
    assert denied[0]["agent_name"] == "x"


def test_denied_collects_task_cancelled_records() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="abort", cascade=False),
    )
    rebuilt = fold(log, cs, "t1")
    denied = rebuilt.governance.denied
    assert len(denied) == 1
    assert denied[0]["type"] == "TaskCancelled"
    # TaskCancelled is a terminal lifecycle event: fold must promote
    # the task to ``terminal`` and clear ``wake_on``.
    assert rebuilt.status == "terminal"
    assert rebuilt.wake_on is None


def test_task_cancelled_clears_wake_on_from_suspended_task() -> None:
    """A cancellation that arrives while the task is suspended must
    clear the persisted ``wake_on`` along with the lifecycle bump."""
    from noeta.protocols.events import TaskSuspendedPayload
    from noeta.protocols.wake import HumanResponseReceived

    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="TaskSuspended",
        payload=TaskSuspendedPayload(
            reason="waiting_human",
            wake_on=HumanResponseReceived(handle="h1"),
        ),
    )
    log.emit(
        task_id="t1",
        type="TaskCancelled",
        payload=TaskCancelledPayload(reason="abort", cascade=False),
    )
    rebuilt = fold(log, cs, "t1")
    assert rebuilt.status == "terminal"
    assert rebuilt.wake_on is None


def test_spawned_subtasks_independent_from_subtask_results() -> None:
    """``spawned_subtasks`` counts ``SubtaskSpawned`` events;
    ``subtask_results`` collects from ``SubtaskCompleted`` events. They
    are independent — partial completions show ``spawned > completed``."""
    log, cs = _make_runtime()
    for i in range(3):
        log.emit(
            task_id="t1",
            type="SubtaskSpawned",
            payload=SubtaskSpawnedPayload(
                subtask_id=f"c{i}", agent_name="child", goal="g"
            ),
        )
    log.system_emit(
        task_id="t1",
        type="SubtaskCompleted",
        payload=SubtaskCompletedPayload(
            subtask_id="c0",
            result=SubtaskResult(status="completed", output=None, error=None),
        ),
        actor="observer",
        origin="observer",
    )
    g = fold(log, cs, "t1").governance
    assert g.spawned_subtasks == 3
    assert len(g.subtask_results) == 1


# ---------------------------------------------------------------------------
# (issue 05) — background-shell jobs folded into governance
# ---------------------------------------------------------------------------


def _bg_ref(tag: str) -> ContentRef:
    return ContentRef(hash=(tag * 64)[:64], size=4, media_type="text/plain")


def test_background_shell_started_appends_running_job() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="npm run dev", spawned_by_task_id="t1",
            pid=10, ref=_bg_ref("a"),
        ),
    )
    jobs = fold(log, cs, "t1").governance.background_jobs
    assert jobs == [
        {
            "job_id": "j1",
            "command": "npm run dev",
            "status": "running",
            "spawned_by_task_id": "t1",
            "ref": _bg_ref("a"),
        }
    ]


def test_background_shell_polled_updates_ref_only() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="srv", spawned_by_task_id="t1",
            pid=1, ref=_bg_ref("a"),
        ),
    )
    log.emit(
        task_id="t1",
        type="BackgroundShellPolled",
        payload=BackgroundShellPolledPayload(job_id="j1", ref=_bg_ref("c"), offset=9),
    )
    jobs = fold(log, cs, "t1").governance.background_jobs
    assert len(jobs) == 1
    assert jobs[0]["status"] == "running"
    assert jobs[0]["ref"] == _bg_ref("c")


def test_background_shell_exited_updates_status_exit_code_and_ref() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="sleep", spawned_by_task_id="t1",
            pid=1, ref=_bg_ref("a"),
        ),
    )
    log.emit(
        task_id="t1",
        type="BackgroundShellExited",
        payload=BackgroundShellExitedPayload(
            job_id="j1", exit_code=3, final_ref=_bg_ref("b"), summary="boom",
        ),
    )
    jobs = fold(log, cs, "t1").governance.background_jobs
    # Audit trail: updated in place, not removed.
    assert len(jobs) == 1
    assert jobs[0]["status"] == "exited"
    assert jobs[0]["exit_code"] == 3
    assert jobs[0]["ref"] == _bg_ref("b")


def test_background_shell_killed_updates_status_and_signal() -> None:
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="BackgroundShellStarted",
        payload=BackgroundShellStartedPayload(
            job_id="j1", command="tail", spawned_by_task_id="t1",
            pid=1, ref=_bg_ref("a"),
        ),
    )
    log.emit(
        task_id="t1",
        type="BackgroundShellKilled",
        payload=BackgroundShellKilledPayload(job_id="j1", signal=9),
    )
    jobs = fold(log, cs, "t1").governance.background_jobs
    assert len(jobs) == 1
    assert jobs[0]["status"] == "killed"
    assert jobs[0]["signal"] == 9


def test_background_shell_poll_on_unknown_job_is_ignored() -> None:
    # Defensive: a poll/exit/kill for a job that never started is a no-op
    # (out-of-order / duplicated stream) rather than a crash.
    log, cs = _make_runtime()
    log.emit(
        task_id="t1",
        type="BackgroundShellPolled",
        payload=BackgroundShellPolledPayload(
            job_id="ghost", ref=_bg_ref("c"), offset=1
        ),
    )
    assert fold(log, cs, "t1").governance.background_jobs == []


def test_old_recording_without_bg_events_folds_empty_and_byte_equal() -> None:
    """Byte-safe: a stream with no BackgroundShell events folds to an empty
    list, and a snapshot serialize→deserialize round-trip is a fixed point —
    the new field never perturbs the canonical bytes of old recordings.

    Mirrors the snapshot byte-equal contract the other GovernanceState lists
    honour: ``background_jobs`` defaults to ``[]`` and is appended LAST.
    """
    from noeta.core.snapshot import deserialize_task_state, serialize_task_state

    log, cs = _make_runtime()
    task = fold(log, cs, "t1")
    assert task.governance.background_jobs == []

    # serialize → deserialize → rehydrate is a fixed point (canonical JSON).
    body = serialize_task_state(task)
    state_dict = deserialize_task_state(body)
    assert state_dict["governance"]["background_jobs"] == []
    rehydrated = fold(log, cs, "t1")  # from-scratch fold of the same stream
    assert serialize_task_state(rehydrated) == body


def test_old_snapshot_dict_missing_field_rehydrates_via_default() -> None:
    """An old snapshot body has no ``background_jobs`` key in its governance
    dict; ``GovernanceState(**governance)`` must rebuild via the field default
    (the 'optional + last' convention) instead of raising."""
    from noeta.core.snapshot import rehydrate_task
    from noeta.protocols.canonical import from_canonical, to_canonical
    from noeta.protocols.task import Task

    fresh = Task(task_id="t1", status="running")
    state_dict = fresh.state_dict()
    # Simulate a pre-issue-05 snapshot body: drop the new key entirely.
    del state_dict["governance"]["background_jobs"]
    # Round-trip through canonical exactly like the real snapshot reader.
    state_dict = from_canonical(to_canonical(state_dict))
    rebuilt = rehydrate_task(state_dict)
    assert rebuilt.governance.background_jobs == []


def test_old_snapshot_runtime_missing_last_input_tokens_defaults_zero() -> None:
    """Compaction byte-safety: a pre-compaction snapshot body has no
    ``last_input_tokens`` key in its runtime dict; ``RuntimeState(**runtime)``
    must rebuild via the field default (0, the 'optional + last' convention)
    instead of raising."""
    from noeta.core.snapshot import rehydrate_task
    from noeta.protocols.canonical import from_canonical, to_canonical
    from noeta.protocols.task import Task

    fresh = Task(task_id="t1", status="running")
    state_dict = fresh.state_dict()
    del state_dict["runtime"]["last_input_tokens"]
    state_dict = from_canonical(to_canonical(state_dict))
    rebuilt = rehydrate_task(state_dict)
    assert rebuilt.runtime.last_input_tokens == 0
