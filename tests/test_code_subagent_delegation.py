"""Phase 4.5 Issue C — typed sub-agent delegation through `noeta code`.

A coding parent calls the model-visible `spawn_subagent(agent, goal)`
control tool; `ReActPolicy` translates it into a `SpawnSubtaskDecision`;
the runner drives a child built from the **named agent's own** config
(isolated system prompt / tools / context), and the result folds back as
a paired `role="tool"` message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.core.fold import fold
from noeta.presets import official_specs
from noeta.policies.react import (
    ReActPolicy,
    SPAWN_SUBAGENT_TOOL,
    spawn_subagent_tool_schema,
)
from noeta.protocols.decisions import (
    SpawnSubtaskDecision,
    StatePatchDecision,
)
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View, ViewSegment
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


SPAWN_CALL_ID = "s1"
PARENT_GOAL = "delegate the review"
CHILD_GOAL = "review x.py for issues"


def _spawn_call(agent: str = "explore", goal: str = CHILD_GOAL) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=SPAWN_CALL_ID,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": SPAWN_CALL_ID},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    return ws


def _system_text(req: LLMRequest) -> str:
    sys_msg = req.system
    if isinstance(sys_msg, Message):
        return " ".join(
            b.text for b in sys_msg.content if isinstance(b, TextBlock)
        )
    return str(sys_msg)


def _session(
    ws: Path, responses: list[LLMResponse], *, spawnable: tuple[str, ...] = ("explore",)
):
    """A one-shot SDK host that may delegate to ``spawnable``.

    ``delegate_to=("explore",)`` maps to ``capabilities.delegation=True`` +
    ``spawnable=("explore",)`` on the main spec (the SDK host reads delegation
    rights off the spec); the named children are registered alongside it.
    Returns ``(host, driver, provider)`` — the shared ``FakeLLMProvider`` carries
    ``received_requests`` for the white-box schema/isolation assertions.
    """
    provider = FakeLLMProvider(responses=responses)
    main = runner_main_spec("main", delegation=True, spawnable=spawnable)
    children = [preset_spec(n) for n in ("explore", "general-purpose", "plan")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
    )
    return host, make_driver(host), provider


def _child_id(host, parent_task_id: str) -> str:
    for env in host.event_log.read(parent_task_id):
        if env.type == "SubtaskSpawned":
            return str(env.payload.subtask_id)
    raise AssertionError("no SubtaskSpawned on parent stream")


# ---------------------------------------------------------------------------
# happy path: delegate → child runs → result folds back
# ---------------------------------------------------------------------------


def test_delegation_runs_child_and_folds_result(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    # parent spawn → child reviews (end_turn) → parent finishes.
    host, driver, _ = _session(ws, [_spawn_call(), _end("looks good"), _end("done")])
    out = driver.start(goal=PARENT_GOAL, agent="main")
    assert out.status == "terminal"
    parent = fold(host.event_log, host.content_store, out.task_id)
    # child result folded into governance + rendered as paired tool result.
    assert parent.governance.subtask_results
    assert parent.governance.subtask_results[-1].output == "looks good"
    tool_msgs = [m for m in parent.runtime.messages if m.role == "tool"]
    paired = [
        b
        for m in tool_msgs
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == SPAWN_CALL_ID
    ]
    assert paired and paired[0].output == "looks good"


def test_result_message_lands_between_woken_and_next_compose(tmp_path: Path) -> None:
    """Architect test #2: the paired tool-result MessagesAppended must sit
    between the subtask ``TaskWoken`` and the next ``ContextPlanComposed``
    so the existing ``_replay_post_wake_segment`` covers it."""
    ws = _make_ws(tmp_path)
    host, driver, _ = _session(ws, [_spawn_call(), _end("reviewed"), _end("done")])
    out = driver.start(goal=PARENT_GOAL, agent="main")
    types = [e.type for e in host.event_log.read(out.task_id)]
    woken = types.index("TaskWoken")
    # the result MessagesAppended is the first MessagesAppended after
    # the wake; the next ContextPlanComposed must follow it.
    appended = types.index("MessagesAppended", woken)
    next_compose = types.index("ContextPlanComposed", woken)
    assert woken < appended < next_compose


def test_spawn_subagent_is_control_surface_not_a_tool(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    host, driver, provider = _session(ws, [_spawn_call(), _end("ok"), _end("done")])
    out = driver.start(goal=PARENT_GOAL, agent="main")
    # in the provider tool schema...
    first_req = provider.received_requests[0]
    tool_names = [t["function"]["name"] for t in first_req.tools]
    assert SPAWN_SUBAGENT_TOOL in tool_names
    # ...but absent from the Engine tools dict, and never executed. The control
    # tool is never a runnable ToolRuntime tool in ANY built engine.
    engine = host.resolve_engine_for_agent("main", model="gpt-test")
    assert SPAWN_SUBAGENT_TOOL not in engine._tools  # type: ignore[attr-defined]
    types = [e.type for e in host.event_log.read(out.task_id)]
    # no ToolCallStarted for spawn_subagent on the parent stream
    # (the only tool events would be from real tools, of which there
    # are none in this script).
    assert "ToolCallStarted" not in types


def test_child_context_isolated_from_parent(tmp_path: Path) -> None:
    """Architect test #1: the child's first LLMRequest has the CHILD
    system prompt + only the child goal — never the parent's prompt or
    messages."""
    ws = _make_ws(tmp_path)
    _, driver, provider = _session(ws, [_spawn_call(), _end("reviewed"), _end("done")])
    driver.start(goal=PARENT_GOAL, agent="main")
    # received_requests[0] = parent spawn turn; [1] = child's first turn.
    child_req: LLMRequest = provider.received_requests[1]
    assert _system_text(child_req) == official_specs()["explore"].instructions
    assert _system_text(child_req) != official_specs()["main"].instructions
    all_text = " ".join(
        b.text
        for m in child_req.messages
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert CHILD_GOAL in all_text
    assert PARENT_GOAL not in all_text


def test_child_uses_own_tool_allowlist(tmp_path: Path) -> None:
    """The read-only explore child cannot write even though the
    parent (main) can — the child's OWN PermissionGuard denies it."""
    ws = _make_ws(tmp_path)
    child_write = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="c-w",
                tool_name="write",
                arguments={"path": "new.py", "content": "x\n"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "c-w"},
    )
    host, driver, _ = _session(
        ws, [_spawn_call(), child_write, _end("can't write"), _end("done")]
    )
    out = driver.start(goal=PARENT_GOAL, agent="main")
    child_id = _child_id(host, out.task_id)
    child_types = [e.type for e in host.event_log.read(child_id)]
    assert "ToolCallDenied" in child_types  # write denied for child
    assert not (ws / "new.py").exists()


def test_deny_unauthorized_subagent(tmp_path: Path) -> None:
    ws = _make_ws(tmp_path)
    # parent tries to delegate to general-purpose, which is NOT in delegate_to.
    host, driver, _ = _session(ws, [_spawn_call(agent="general-purpose")])
    out = driver.start(goal=PARENT_GOAL, agent="main")
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "SubtaskDenied" in types
    assert out.status in ("terminal", "failed")
    # no child task was created.
    assert not any(t == "SubtaskSpawned" for t in types) or all(
        e.type != "TaskCreated"
        for e in host.event_log.read(out.task_id)
        if e.seq > 0 and e.type == "TaskCreated"
    )


def test_unknown_delegate_to_denies(tmp_path: Path) -> None:
    """P1 regression: an unknown ``--delegate-to`` agent must hard-deny.
    The allow-list is filtered through the registry (``_spawnable_set``), so the
    model calling ``spawn_subagent(agent="unknown")`` yields
    ``SubtaskDenied`` + parent failure (no child)."""
    ws = _make_ws(tmp_path)
    # spawnable names an unregistered agent: delegation is enabled but the
    # authorized set folds to empty, so the spawn is denied.
    host, driver, _ = _session(
        ws, [_spawn_call(agent="ghost")], spawnable=("ghost",)
    )
    out = driver.start(goal=PARENT_GOAL, agent="main")
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "SubtaskDenied" in types
    assert "SubtaskSpawned" not in types  # no child created


# ---------------------------------------------------------------------------
# ReActPolicy translation unit (mixed batch fail-closed)
# ---------------------------------------------------------------------------


class _OneShotLLM:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    def complete(
        self, req: LLMRequest, ctx: StepContext, *, selection: Any = None
    ) -> LLMResponse:
        return self._response


def _view() -> View:
    # A minimal but well-formed 3-segment view: _build_request reads
    # segments[0].content[0] as the system message.
    return View(
        plan_ref=None,
        segments=(
            ViewSegment(
                name="stable_prefix",
                content=[Message(role="system", content=[TextBlock(text="p")])],
                segment_hash="h",
            ),
            ViewSegment(name="semi_stable", content=[], segment_hash="h"),
            ViewSegment(name="dynamic_suffix", content=[], segment_hash="h"),
        ),
        provider_tool_schemas=[],
    )


def test_single_spawn_translates_to_spawn_decision() -> None:
    llm = _OneShotLLM(_spawn_call(agent="explore"))
    policy = ReActPolicy(
        llm=llm, tools={}, system_prompt="p", model="m", delegation_enabled=True
    )
    decision = policy.decide(StepContext(task_id="t", lease_id="l", trace_id="tr"), _view())
    assert isinstance(decision, SpawnSubtaskDecision)
    assert decision.agent_name == "explore"
    assert decision.goal == CHILD_GOAL


def test_mixed_spawn_batch_returns_recoverable_ack() -> None:
    mixed = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="a", tool_name=SPAWN_SUBAGENT_TOOL, arguments={"agent": "explore", "goal": "g"}),
            ToolUseBlock(call_id="b", tool_name="read_file", arguments={"path": "x"}),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "m"},
    )
    policy = ReActPolicy(
        llm=_OneShotLLM(mixed), tools={}, system_prompt="p", model="m",
        delegation_enabled=True,
    )
    decision = policy.decide(StepContext(task_id="t", lease_id="l", trace_id="tr"), _view())
    # Recoverable error ack — task continues, model may retry.
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    assert ack.role == "tool"
    assert len(ack.content) == 2
    call_ids = sorted(b.call_id for b in ack.content)
    assert call_ids == ["a", "b"]
    for b in ack.content:
        assert isinstance(b, ToolResultBlock)
        assert b.success is False
        assert b.error is not None
        assert "spawn_subagent cannot be mixed with other tool calls" in b.output


def test_delegation_disabled_treats_spawn_as_normal_tool() -> None:
    policy = ReActPolicy(
        llm=_OneShotLLM(_spawn_call()), tools={}, system_prompt="p", model="m",
        delegation_enabled=False,
    )
    decision = policy.decide(StepContext(task_id="t", lease_id="l", trace_id="tr"), _view())
    # not translated — falls through to a normal ToolCallsDecision.
    assert not isinstance(decision, SpawnSubtaskDecision)


