"""SR1 — bounded recursive delegation.

Covers the spec gates: depth-scalar recording + old-shape→0 restore
(TaskCreatedPayload / fold genesis / snapshot / sqlite); the BudgetGuard
``max_subtask_depth`` semantics; an end-to-end nested
``root → child → grandchild`` ``noeta code`` run with correct depths;
a depth-cap deny landing *before*
child creation with a deterministic event sequence; B1 child delegation
inheritance (the child actually spawns a grandchild); B2 targeted child
lease (a decoy ready task is never driven); and B3 unsupported child
suspend releasing the lease then raising a typed error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.core.fold import fold
from noeta.core.snapshot import rehydrate_task
from noeta.execution.subtask_drain import UnsupportedSubtaskSuspend
from noeta.guards.budget import Budget, BudgetGuard
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.decisions import SpawnSubtaskDecision
from noeta.protocols.events import TaskCreatedPayload
from noeta.protocols.hooks import GuardContext, ProposedSpawnSubtask
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.protocols.wake import HumanResponseReceived, SubtaskCompleted
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.storage.sqlite.eventlog import SqliteEventLog
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    coding_replay_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


# ---------------------------------------------------------------------------
# scripted LLM helpers
# ---------------------------------------------------------------------------


def _spawn(agent: str, goal: str, call_id: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    return ws


def _session(
    ws: Path,
    responses: list[LLMResponse],
    *,
    delegate_to: tuple[str, ...] = ("explore", "general-purpose"),
    max_subtask_depth: int | None = 3,
):
    """A one-shot SDK host that may recursively delegate to ``delegate_to``.

    ``delegate_to=(...)`` → ``capabilities.delegation=True`` + ``spawnable=(...)``
    on the main spec (children inherit delegation through the drain);
    ``max_subtask_depth`` rides the host Budget exactly like the old runner's
    ``coding_replay_budget(max_subtask_depth)``. Returns ``(host, driver,
    provider)`` — the shared ``FakeLLMProvider`` carries ``received_requests``.
    """
    provider = FakeLLMProvider(responses=responses)
    main = runner_main_spec("main", delegation=True, spawnable=delegate_to)
    children = [preset_spec(n) for n in delegate_to]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        budget=coding_replay_budget(max_subtask_depth),
    )
    return host, make_driver(host), provider


# ---------------------------------------------------------------------------
# 1. depth scalar: old-shape restores to 0 (gate 1)
# ---------------------------------------------------------------------------


def test_taskcreated_payload_defaults_depth_zero() -> None:
    p = TaskCreatedPayload(goal="g", policy_name="react")
    assert p.subtask_depth == 0


def test_fold_genesis_without_depth_is_zero() -> None:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    log.emit(task_id="t1", type="TaskCreated",
             payload=TaskCreatedPayload(goal="g", policy_name="react"))
    task = fold(log, cs, "t1")
    assert task.subtask_depth == 0


def test_snapshot_roundtrip_without_depth_is_zero() -> None:
    # an old snapshot body has no subtask_depth key → rehydrate to 0
    task = rehydrate_task({
        "task_id": "t1", "status": "running", "parent_task_id": None,
        "runtime": {"messages": []}, "state": {}, "context": {},
        "governance": {}, "wake_on": None,
    })
    assert task.subtask_depth == 0


def test_sqlite_roundtrip_taskcreated_depth(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "k.db")
    try:
        log.emit(task_id="t1", type="TaskCreated",
                 payload=TaskCreatedPayload(
                     goal="g", policy_name="scripted", subtask_depth=2))
        env = log.read("t1")[0]
        assert env.payload.subtask_depth == 2
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 2. BudgetGuard depth semantics (gate 8)
# ---------------------------------------------------------------------------


def _spawn_verdict(*, current_depth: int, cap: int | None) -> Any:
    guard = BudgetGuard(Budget(max_subtask_depth=cap))
    ctx = GuardContext(task_id="t", subtask_depth=current_depth)
    action = ProposedSpawnSubtask(
        decision=SpawnSubtaskDecision(agent_name="a", goal="g")
    )
    return guard.check(action, ctx)


def test_depth_guard_allows_under_cap() -> None:
    # root (depth 0), cap 1 → may spawn a child (depth 1)
    assert _spawn_verdict(current_depth=0, cap=1).verdict.name == "ALLOW"


def test_depth_guard_denies_at_cap() -> None:
    # child (depth 1), cap 1 → may NOT spawn a grandchild (depth 2)
    v = _spawn_verdict(current_depth=1, cap=1)
    assert v.verdict.name == "DENY"
    assert "max_subtask_depth=1" in (v.reason or "")


def test_depth_guard_none_is_unlimited() -> None:
    assert _spawn_verdict(current_depth=99, cap=None).verdict.name == "ALLOW"


# ---------------------------------------------------------------------------
# 3. end-to-end nested: root → child → grandchild (gates 2, 7)
# ---------------------------------------------------------------------------


def _depth_of(host, task_id: str) -> int:
    created = [e for e in host.event_log.read(task_id) if e.type == "TaskCreated"][0]
    return int(created.payload.subtask_depth)


def _spawned_child(host, parent_id: str) -> str:
    for e in host.event_log.read(parent_id):
        if e.type == "SubtaskSpawned":
            return str(e.payload.subtask_id)
    raise AssertionError(f"no SubtaskSpawned on {parent_id}")


def test_nested_delegation_records_depths(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    # call order across the shared provider: parent spawns code-reviewer,
    # that child spawns bug-fixer, grandchild ends, child ends, parent ends.
    host, driver, _ = _session(ws, [
        _spawn("explore", "review", "c1"),
        _spawn("general-purpose", "fix", "c2"),
        _end("grandchild done"),
        _end("child done"),
        _end("parent done"),
    ])
    out = driver.start(goal="root goal", agent="main")
    assert out.status == "terminal"
    root_id = out.task_id
    child_id = _spawned_child(host, root_id)
    grandchild_id = _spawned_child(host, child_id)

    # gate 7 — depths recorded on each TaskCreated.
    assert _depth_of(host, root_id) == 0
    assert _depth_of(host, child_id) == 1
    assert _depth_of(host, grandchild_id) == 2
    # folded onto each Task too.
    assert fold(host.event_log, host.content_store, grandchild_id).subtask_depth == 2


# ---------------------------------------------------------------------------
# 4. depth cap denies grandchild BEFORE creation (gate 3)
# ---------------------------------------------------------------------------


def test_depth_cap_denies_grandchild_before_creation(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    # max_subtask_depth=1: root(0)→child(1) allowed; child(1)→grandchild(2)
    # DENIED. The child's spawn attempt fails it; the parent resumes with a
    # failed subtask result and finishes.
    host, driver, _ = _session(ws, [
        _spawn("explore", "review", "c1"),
        _spawn("general-purpose", "fix", "c2"),   # denied at the child (depth 1)
        _end("parent handled failure"),
    ], max_subtask_depth=1)
    out = driver.start(goal="root goal", agent="main")
    assert out.status == "terminal"
    root_id = out.task_id
    child_id = _spawned_child(host, root_id)
    child_types = [e.type for e in host.event_log.read(child_id)]
    # deny happened: SubtaskDenied + TaskFailed, and crucially NO
    # SubtaskSpawned (no grandchild was ever created).
    assert "SubtaskDenied" in child_types
    assert "TaskFailed" in child_types
    assert "SubtaskSpawned" not in child_types
    # deterministic ordering: SubtaskDenied precedes TaskFailed.
    assert child_types.index("SubtaskDenied") < child_types.index("TaskFailed")
    # the parent saw a FAILED subtask result.
    parent = fold(host.event_log, host.content_store, root_id)
    assert parent.governance.subtask_results[-1].status == "failed"


# ---------------------------------------------------------------------------
# 5. B1 — child inherits delegation capability (gate 9)
# ---------------------------------------------------------------------------


def test_child_inherits_delegation_and_can_spawn(tmp_path: Path) -> None:
    """The nested run only completes because the CHILD engine was built
    WITH the spawn_subagent schema (B1). A grandchild stream existing is
    direct proof the child could delegate."""
    ws = _ws(tmp_path)
    host, driver, _ = _session(ws, [
        _spawn("explore", "review", "c1"),
        _spawn("general-purpose", "fix", "c2"),
        _end("g"), _end("c"), _end("p"),
    ])
    out = driver.start(goal="root goal", agent="main")
    child_id = _spawned_child(host, out.task_id)
    # the child produced its OWN SubtaskSpawned → it had the schema.
    child_types = [e.type for e in host.event_log.read(child_id)]
    assert "SubtaskSpawned" in child_types
    # boundary: the child engine's tools never include an MCP tool (MCP is not
    # inherited) — the child build passes no mcp specs.
    child_engine = host.resolve_engine_for_agent("explore", model="gpt-test")
    assert not any(
        n.startswith("mcp__") for n in child_engine._tools
    )


# ---------------------------------------------------------------------------
# 6. B2 — targeted child lease never grabs a decoy ready task (gate 10)
# ---------------------------------------------------------------------------


def test_targeted_child_lease_ignores_decoy_ready_task(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    host, driver, _ = _session(ws, [_spawn("explore", "review", "c1"),
                                    _end("child done"), _end("parent done")])
    # Inject a decoy ready task AHEAD of the (not-yet-created) child. A
    # non-targeted "next ready" lease would grab this; a targeted lease
    # (by wake_on.subtask_id) must not. The root session is leased targeted by
    # the driver, so the decoy never perturbs it either.
    host.event_log.system_emit(
        task_id="decoy-task", type="TaskCreated",
        payload=TaskCreatedPayload(goal="decoy", policy_name="scripted"),
        actor="test", origin="engine", trace_id="t",
    )
    host.dispatcher.enqueue("decoy-task")
    out = driver.start(goal="root goal", agent="main")
    # the real child ran to terminal …
    child_id = _spawned_child(host, out.task_id)
    assert any(
        e.type == "TaskCompleted"
        for e in host.event_log.read(child_id)
    )
    # … and the decoy was NEVER driven (still leasable / no further events).
    decoy_lease = host.dispatcher.lease(worker_id="probe", task_id="decoy-task")
    assert decoy_lease is not None  # still ready → never consumed
    assert [e.type for e in host.event_log.read("decoy-task")] == ["TaskCreated"]


# ---------------------------------------------------------------------------
# 7. B3 — unsupported child suspend releases the lease, then raises (gate 11)
# ---------------------------------------------------------------------------


def test_unsupported_child_suspend_releases_lease_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _ws(tmp_path)
    host, driver, _ = _session(ws, [_spawn("explore", "review", "c1"), _end("p")])

    real_build_engine = host._build_engine

    class _SuspendingEngine:
        """Wraps the real child engine but forces its run_one_step to leave
        the child suspended on a non-SubtaskCompleted (human) wake."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def append_user_message(self, task: Any, **kw: Any) -> Any:
            return self._inner.append_user_message(task, **kw)

        def run_one_step(self, task: Any, **kw: Any) -> Any:
            task = self._inner.run_one_step(task, **kw)
            task.status = "suspended"
            task.wake_on = HumanResponseReceived(handle="needs-human")
            return task

    def _suspending_build(agent: Any, model: str, **kw: Any) -> Any:
        engine = real_build_engine(agent, model, **kw)
        # Wrap ONLY the delegated child engine; the root ``main`` engine (already
        # cached by the seed) drives the spawn normally. Forcing the child to a
        # human suspend is exactly what the drain rejects.
        if agent.name != "main":
            return _SuspendingEngine(engine)
        return engine

    monkeypatch.setattr(host, "_build_engine", _suspending_build)

    seeded = driver.seed_start(goal="root goal", agent="main")
    with pytest.raises(UnsupportedSubtaskSuspend) as exc:
        driver.drive_seeded(seeded)
    assert isinstance(exc.value.wake_on, HumanResponseReceived)
    child_id = _spawned_child(host, seeded.task_id)
    assert exc.value.task_id == child_id
    # B3: the child lease was RELEASED (suspended state) before the
    # raise — NOT leaked/held. Proof: a freshly-leased ``task_id`` for a
    # still-held lease would fail; here, waking the released-suspended
    # child makes it ready and it leases cleanly.
    host.dispatcher.wake(child_id, HumanResponseReceived(handle="needs-human"))
    relead = host.dispatcher.lease(worker_id="probe", task_id=child_id)
    assert relead is not None and relead.task_id == child_id


# ---------------------------------------------------------------------------
# 8. no cross-task request coupling: depth never enters an LLMRequest (gate 6)
# ---------------------------------------------------------------------------


def test_depth_never_appears_in_any_llm_request(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(responses=[
        _spawn("explore", "review", "c1"),
        _spawn("general-purpose", "fix", "c2"),
        _end("g"), _end("c"), _end("p"),
    ])
    main = runner_main_spec(
        "main", delegation=True, spawnable=("explore", "general-purpose")
    )
    children = [preset_spec(n) for n in ("explore", "general-purpose")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        budget=coding_replay_budget(3),
    )
    driver = make_driver(host)
    driver.start(goal="root", agent="main")
    # subtask_depth is event/state metadata only — it must never be part
    # of the prompt material sent to the provider.
    for req in provider.received_requests:
        for m in req.messages:
            for b in m.content:
                assert "subtask_depth" not in str(getattr(b, "text", ""))
