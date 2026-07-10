"""SR2 — parallel fan-out / N-way subtask-group join.

Covers the spec gates: fan-out happy path + member-order results; the
`SubtaskGroupCompleted` wake variant + group_id derivation; distinct-
membership join (group wake fires only on the last member); depth compose;
all-or-none batch admission (size cap / duplicate call_id / budget k-th
fail / approval-unsupported → zero child); the typed guard simulated-
increment seam; positional call_id pairing (incl. a prior paired single
spawn); and SR1 zero-regression (single spawn stays SpawnSubtaskDecision).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.guards.budget import Budget, BudgetGuard
from noeta.policies.react import ReActPolicy, SPAWN_SUBAGENT_TOOL
from noeta.protocols.decisions import (
    SpawnSubtaskDecision,
    SpawnSubtasksDecision,
    StatePatchDecision,
)
from noeta.protocols.hooks import GuardContext, ProposedSpawnSubtask
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.decisions import SpawnSubtaskDecision as _Single
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View, ViewSegment
from noeta.protocols.wake import (
    SubtaskCompleted,
    SubtaskGroupCompleted,
    derive_group_id,
    matches_wake,
)
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
# helpers
# ---------------------------------------------------------------------------


def _spawn_pair(*pairs: tuple[str, str, str]) -> LLMResponse:
    """One assistant turn with N spawn_subagent tool_uses.
    Each pair = (call_id, agent, goal)."""
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id=c, tool_name=SPAWN_SUBAGENT_TOOL,
                         arguments={"agent": a, "goal": g})
            for (c, a, g) in pairs
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "sp"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn", content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1), raw={"id": "e"},
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
    budget: Budget | None = None,
):
    """A one-shot SDK host that fans out to ``delegate_to``.

    ``delegate_to=(...)`` maps to ``capabilities.delegation=True`` +
    ``spawnable=(...)`` on the main spec; the named children are registered
    alongside it. ``budget=None`` mirrors the old runner default
    (``coding_replay_budget(max_subtask_depth=3)``); an explicit budget is
    authoritative. Returns ``(host, driver)``.
    """
    main = runner_main_spec("main", delegation=True, spawnable=delegate_to)
    children = [preset_spec(n) for n in delegate_to]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        budget=budget if budget is not None else coding_replay_budget(3),
    )
    return host, make_driver(host)


def _spawned(host, parent_id: str) -> list[str]:
    return [
        str(e.payload.subtask_id)
        for e in host.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    ]


def _types(host, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


# ---------------------------------------------------------------------------
# 1. L0 — wake variant + group_id derivation + matches_wake (units)
# ---------------------------------------------------------------------------


def test_derive_group_id_is_deterministic_and_order_sensitive() -> None:
    assert derive_group_id(("a", "b")) == derive_group_id(("a", "b"))
    assert derive_group_id(("a", "b")) != derive_group_id(("b", "a"))
    assert derive_group_id(("a", "b")).startswith("g-")


def test_matches_wake_projects_on_group_id() -> None:
    cond = SubtaskGroupCompleted(group_id="g-x", subtask_ids=("a", "b"))
    # different subtask_ids, same group_id → match (subtask_ids informational)
    evt = SubtaskGroupCompleted(group_id="g-x", subtask_ids=())
    assert matches_wake(cond, evt) is True
    assert matches_wake(cond, SubtaskGroupCompleted(group_id="g-y", subtask_ids=("a", "b"))) is False
    # cross-variant never matches
    assert matches_wake(cond, SubtaskCompleted(subtask_id="a")) is False


# ---------------------------------------------------------------------------
# 2. policy routing: 1 → single, >=2 → batch, mixed → fail (SR1 zero-regress)
# ---------------------------------------------------------------------------


class _OneShot:
    def __init__(self, resp: LLMResponse) -> None:
        self._resp = resp

    def complete(self, req: Any, ctx: Any, *, selection: Any = None) -> LLMResponse:
        return self._resp


def _view() -> View:
    return View(
        plan_ref=None,
        segments=(
            ViewSegment(name="stable_prefix",
                        content=[Message(role="system", content=[TextBlock(text="p")])],
                        segment_hash="h0"),
            ViewSegment(name="semi_stable", content=[], segment_hash="h1"),
            ViewSegment(name="dynamic_suffix", content=[], segment_hash="h2"),
        ),
        provider_tool_schemas=[],
    )


def _decide(resp: LLMResponse) -> Any:
    policy = ReActPolicy(
        llm=_OneShot(resp), tools={}, system_prompt="p", model="m",
        delegation_enabled=True,
    )
    return policy.decide(StepContext(task_id="t", lease_id="l", trace_id="tr"), _view())


def test_single_spawn_routes_to_spawn_subtask_decision() -> None:
    d = _decide(_spawn_pair(("a", "explore", "g")))
    assert isinstance(d, SpawnSubtaskDecision)  # SR1 path, zero-regression


def test_two_spawns_route_to_batch_decision_member_order() -> None:
    d = _decide(_spawn_pair(("a", "explore", "ga"), ("b", "general-purpose", "gb")))
    assert isinstance(d, SpawnSubtasksDecision)
    assert [s.call_id for s in d.specs] == ["a", "b"]            # member order
    assert [s.agent_name for s in d.specs] == ["explore", "general-purpose"]


def test_two_spawns_concurrent_by_default(monkeypatch) -> None:
    """A one-turn >=2 spawn fan-out marks the batch decision concurrent unless the
    escape valve forces sequential. Unset env ⇒ concurrent."""
    monkeypatch.delenv("NOETA_SUBTASK_CONCURRENCY", raising=False)
    d = _decide(_spawn_pair(("a", "explore", "ga"), ("b", "general-purpose", "gb")))
    assert isinstance(d, SpawnSubtasksDecision)
    assert d.concurrent is True


def test_two_spawns_sequential_when_escape_valve_set(monkeypatch) -> None:
    monkeypatch.setenv("NOETA_SUBTASK_CONCURRENCY", "0")
    d = _decide(_spawn_pair(("a", "explore", "ga"), ("b", "general-purpose", "gb")))
    assert isinstance(d, SpawnSubtasksDecision)
    assert d.concurrent is False


def test_spawn_mixed_with_nonspawn_returns_recoverable_ack() -> None:
    resp = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="a", tool_name=SPAWN_SUBAGENT_TOOL,
                         arguments={"agent": "explore", "goal": "g"}),
            ToolUseBlock(call_id="b", tool_name="read_file", arguments={"path": "x"}),
        ],
        usage=Usage(uncached=1, output=1), raw={"id": "m"},
    )
    d = _decide(resp)
    # Recoverable ack: task keeps running, model may retry.
    assert isinstance(d, StatePatchDecision)
    assert d.patch is None
    assert len(d.messages_after) == 1
    ack = d.messages_after[0]
    assert len(ack.content) == 2
    call_ids = sorted(b.call_id for b in ack.content)
    assert call_ids == ["a", "b"]
    for b in ack.content:
        assert isinstance(b, ToolResultBlock)
        assert b.success is False
        assert b.error is not None
        assert "spawn_subagent cannot be mixed with other tool calls" in b.output


# ---------------------------------------------------------------------------
# 3. typed guard simulated-increment seam (B2)
# ---------------------------------------------------------------------------


def test_budget_guard_uses_spawned_subtasks_override() -> None:
    guard = BudgetGuard(Budget(max_spawned_subtasks=2))
    action = ProposedSpawnSubtask(decision=_Single(agent_name="a", goal="g"))
    # current=0 simulated as 2 (== cap) → deny
    ctx = GuardContext(task_id="t", subtask_depth=0)
    # mimic Engine._guard override by passing a context whose governance
    # already reflects the simulated count:
    from dataclasses import replace
    from noeta.protocols.task import GovernanceState
    ctx2 = replace(ctx, governance=GovernanceState(spawned_subtasks=2))
    assert guard.check(action, ctx2).verdict.name == "DENY"
    ctx1 = replace(ctx, governance=GovernanceState(spawned_subtasks=1))
    assert guard.check(action, ctx1).verdict.name == "ALLOW"


# ---------------------------------------------------------------------------
# 4. end-to-end fan-out happy path + member-order results + depth (gates 1,5)
# ---------------------------------------------------------------------------


def test_fanout_happy_path_member_order_results_and_depth(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _spawn_pair(("a", "explore", "review A"), ("b", "general-purpose", "fix B")),
        _end("A done"), _end("B done"), _end("parent done"),
    ])
    out = driver.start(goal="root", agent="main")
    assert out.status == "terminal"
    root = out.task_id
    members = _spawned(host, root)
    assert len(members) == 2
    # parent suspended on a GROUP
    susp = [e for e in host.event_log.read(root) if e.type == "TaskSuspended"]
    assert any(isinstance(e.payload.wake_on, SubtaskGroupCompleted) for e in susp)
    # member-order paired results
    parent = fold(host.event_log, host.content_store, root)
    blocks = [
        b for m in parent.runtime.messages if m.role == "tool"
        for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert [(b.call_id, b.output) for b in blocks] == [("a", "A done"), ("b", "B done")]
    # depth 1 for both members
    for sid in members:
        d = [e.payload.subtask_depth for e in host.event_log.read(sid)
             if e.type == "TaskCreated"][0]
        assert d == 1


def test_group_wake_fires_only_after_last_member(tmp_path: Path) -> None:
    """Distinct membership (B1): exactly one TaskWoken on the parent, and it
    follows BOTH members' SubtaskCompleted (the join is an all-of barrier)."""
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _spawn_pair(("a", "explore", "A"), ("b", "general-purpose", "B")),
        _end("A"), _end("B"), _end("done"),
    ])
    out = driver.start(goal="root", agent="main")
    types = _types(host, out.task_id)
    assert types.count("TaskWoken") == 1                 # single join wake
    # both SubtaskCompleted precede the woken
    woken_i = types.index("TaskWoken")
    completed = [i for i, t in enumerate(types) if t == "SubtaskCompleted"]
    assert len(completed) == 2 and max(completed) < woken_i


# ---------------------------------------------------------------------------
# 5. all-or-none admission (B3, B6): duplicate call_id / size / budget
# ---------------------------------------------------------------------------


def _assert_zero_child(host, task_id: str) -> None:
    types = _types(host, task_id)
    assert "SubtaskDenied" in types
    assert "TaskFailed" in types
    assert "SubtaskSpawned" not in types  # ZERO child created


def test_duplicate_call_id_denies_whole_batch(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _spawn_pair(("dup", "explore", "A"), ("dup", "general-purpose", "B")),
    ])
    out = driver.start(goal="root", agent="main")
    _assert_zero_child(host, out.task_id)
    denied = [e for e in host.event_log.read(out.task_id)
              if e.type == "SubtaskDenied"][0]
    assert denied.payload.reason == "fanout_batch_duplicate_call_id"


def test_size_cap_denies_whole_batch(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    # 17 > MAX_FANOUT(16)
    pairs = tuple((f"c{i}", "explore", f"g{i}") for i in range(17))
    host, driver = _session(ws, [_spawn_pair(*pairs)])
    out = driver.start(goal="root", agent="main")
    _assert_zero_child(host, out.task_id)
    denied = [e for e in host.event_log.read(out.task_id)
              if e.type == "SubtaskDenied"][0]
    assert denied.payload.reason == "fanout_batch_size:17>16"


def test_budget_kth_failure_denies_whole_batch(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    # max_spawned_subtasks=1: simulated current+i means the 2nd spec (i=1)
    # sees spawned=1 >= 1 → deny → whole batch denied, zero child.
    host, driver = _session(
        ws,
        [_spawn_pair(("a", "explore", "A"), ("b", "general-purpose", "B"))],
        budget=Budget(max_iterations=15, max_tool_calls=30, max_spawned_subtasks=1),
    )
    out = driver.start(goal="root", agent="main")
    _assert_zero_child(host, out.task_id)


# ---------------------------------------------------------------------------
# 6. positional pairing with a PRIOR paired single spawn (gate 14)
# ---------------------------------------------------------------------------


def test_pairing_unaffected_by_prior_paired_single_spawn(tmp_path: Path) -> None:
    """A parent that first does a single spawn (SR1, gets its tool_result),
    then fans out N: the group's N tool_results pair only to the group's
    call_ids, never to the older (already-paired) single-spawn call_id."""
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _spawn_pair(("single", "explore", "first")),   # 1 → SR1 single
        _end("single done"),                                  # child of single
        _spawn_pair(("a", "explore", "A"), ("b", "general-purpose", "B")),  # then fan-out
        _end("A done"), _end("B done"),
        _end("parent done"),
    ])
    out = driver.start(goal="root", agent="main")
    assert out.status == "terminal"
    parent = fold(host.event_log, host.content_store, out.task_id)
    blocks = [
        (b.call_id, b.output) for m in parent.runtime.messages if m.role == "tool"
        for b in m.content if isinstance(b, ToolResultBlock)
    ]
    # the single result + the two group results, each correctly paired
    assert ("single", "single done") in blocks
    assert ("a", "A done") in blocks
    assert ("b", "B done") in blocks


# ---------------------------------------------------------------------------
# 7. batch form — ONE call carrying a `spawns` array (the shape gpt-5.x
#    actually batches; multiple spawn calls per turn stay supported)
# ---------------------------------------------------------------------------


def _batch_spawn(call_id: str, *pairs: tuple[str, str]) -> LLMResponse:
    """One assistant turn with ONE spawn_subagent tool_use carrying a
    ``spawns`` array. Each pair = (agent, goal)."""
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"spawns": [{"agent": a, "goal": g} for (a, g) in pairs]},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "bsp"},
    )


def test_batch_call_routes_to_batch_decision_shared_call_id() -> None:
    d = _decide(_batch_spawn("c", ("explore", "ga"), ("general-purpose", "gb")))
    assert isinstance(d, SpawnSubtasksDecision)
    assert [s.call_id for s in d.specs] == ["c", "c"]
    assert [s.member_index for s in d.specs] == [0, 1]
    assert [s.agent_name for s in d.specs] == ["explore", "general-purpose"]
    assert [s.goal for s in d.specs] == ["ga", "gb"]


def test_batch_call_single_entry_stays_sr1() -> None:
    d = _decide(_batch_spawn("c", ("explore", "g")))
    assert isinstance(d, SpawnSubtaskDecision)
    assert d.agent_name == "explore" and d.goal == "g"


def test_batch_call_mixed_with_legacy_call_flattens_in_order() -> None:
    resp = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="b1", tool_name=SPAWN_SUBAGENT_TOOL,
                         arguments={"spawns": [
                             {"agent": "explore", "goal": "g0"},
                             {"agent": "explore", "goal": "g1"},
                         ]}),
            ToolUseBlock(call_id="b2", tool_name=SPAWN_SUBAGENT_TOOL,
                         arguments={"agent": "general-purpose", "goal": "g2"}),
        ],
        usage=Usage(uncached=1, output=1), raw={"id": "mx"},
    )
    d = _decide(resp)
    assert isinstance(d, SpawnSubtasksDecision)
    assert [(s.call_id, s.member_index, s.goal) for s in d.specs] == [
        ("b1", 0, "g0"), ("b1", 1, "g1"), ("b2", 0, "g2"),
    ]


def test_malformed_spawns_returns_recoverable_ack() -> None:
    for bad in ([], "not-an-array", [{"agent": "explore"}], [42]):
        resp = LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(call_id="c", tool_name=SPAWN_SUBAGENT_TOOL,
                                  arguments={"spawns": bad})],
            usage=Usage(uncached=1, output=1), raw={"id": "bad"},
        )
        d = _decide(resp)
        assert isinstance(d, StatePatchDecision), bad
        assert d.patch is None
        block = d.messages_after[0].content[0]
        assert isinstance(block, ToolResultBlock) and block.success is False
        assert "non-empty array" in block.output


def test_batch_fanout_e2e_one_aggregated_tool_result(tmp_path: Path) -> None:
    """E2E: one call with 2 spawns → 2 real children, a group suspend, and on
    resume exactly ONE ToolResultBlock (wire correctness: one result per
    call_id) whose output lists both member results in entry order."""
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _batch_spawn("batch", ("explore", "review A"), ("general-purpose", "fix B")),
        _end("A done"), _end("B done"), _end("parent done"),
    ])
    out = driver.start(goal="root", agent="main")
    assert out.status == "terminal"
    root = out.task_id
    assert len(_spawned(host, root)) == 2
    susp = [e for e in host.event_log.read(root) if e.type == "TaskSuspended"]
    assert any(isinstance(e.payload.wake_on, SubtaskGroupCompleted) for e in susp)
    parent = fold(host.event_log, host.content_store, root)
    blocks = [
        b for m in parent.runtime.messages if m.role == "tool"
        for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert len(blocks) == 1
    block = blocks[0]
    assert block.call_id == "batch" and block.success is True
    assert block.output == [
        {"spawn": 0, "success": True, "output": "A done"},
        {"spawn": 1, "success": True, "output": "B done"},
    ]


def test_noncontiguous_duplicate_call_id_still_denied(tmp_path: Path) -> None:
    """The layout guard still rejects a call_id reappearing in a later run
    ([dup, other, dup]) — only contiguous same-call batch members may share."""
    ws = _ws(tmp_path)
    host, driver = _session(ws, [
        _spawn_pair(("dup", "explore", "A"), ("other", "general-purpose", "B"),
                    ("dup", "explore", "C")),
    ])
    out = driver.start(goal="root", agent="main")
    _assert_zero_child(host, out.task_id)
    denied = [e for e in host.event_log.read(out.task_id)
              if e.type == "SubtaskDenied"][0]
    assert denied.payload.reason == "fanout_batch_duplicate_call_id"


def test_spawn_schema_advertises_batch_form() -> None:
    from noeta.policies.control_semantics import spawn_subagent_tool_schema

    schema = spawn_subagent_tool_schema((("explore", "read-only scout"),))
    params = schema["function"]["parameters"]
    assert params["required"] == ["spawns"]
    items = params["properties"]["spawns"]["items"]
    assert items["required"] == ["agent", "goal"]
    # the roster enum threads into each entry's agent property
    assert items["properties"]["agent"]["enum"] == ["explore"]
