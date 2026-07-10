"""`skill` control-tool policy translation.

Unit tests cover the four translation paths (success / unknown name /
sole-call violation / duplicate). Engine-level tests verify the full chain:
model invocation → StatePatchDecision → TaskState.active_skills → next
compose semi-stable body → ContextPlan.selected_skills.
"""

from __future__ import annotations

from pathlib import Path

from tests._skill_fixtures import write_skill

from noeta.core.fold import fold
from noeta.policies._control_translate import (
    SKILL_TOOL,
    ControlToggles,
    translate_control_tool,
)
from noeta.policies.react import (
    SKILL_TOOL as _REACT_SKILL_TOOL,
)
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.decisions import StatePatchDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MENU = frozenset({"alpha", "beta", "gamma"})


def _skill_call(skill_name: str, call_id: str = "sk") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=SKILL_TOOL,
                arguments={"skill": skill_name},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _mixed_skill_and_other_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="s1",
                tool_name="spawn_subagent",
                arguments={"agent": "main", "goal": "child"},
            ),
            ToolUseBlock(
                call_id="sk",
                tool_name=SKILL_TOOL,
                arguments={"skill": "alpha"},
            ),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "mixed"},
    )


def _two_skill_calls() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="sk1",
                tool_name=SKILL_TOOL,
                arguments={"skill": "alpha"},
            ),
            ToolUseBlock(
                call_id="sk2",
                tool_name=SKILL_TOOL,
                arguments={"skill": "beta"},
            ),
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "two"},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _assistant_message(response: LLMResponse) -> Message:
    """Stripped assistant message matching what ReActPolicy builds."""
    return Message(
        role="assistant",
        content=[
            b
            for b in response.content
            if not isinstance(b, ThinkingBlock)
        ],
    )


def _translate(response: LLMResponse, menu: frozenset[str] = _MENU):
    return translate_control_tool(
        response,
        _assistant_message(response),
        toggles=ControlToggles(skill_invocation=True),
        skill_menu_names=menu,
    )


# ---------------------------------------------------------------------------
# Sanity — constant re-export parity and schema visibility
# ---------------------------------------------------------------------------


def test_skill_tool_constant_parity() -> None:
    """SKILL_TOOL is the same constant on both sides of the seam."""
    assert SKILL_TOOL == "skill"
    assert _REACT_SKILL_TOOL == SKILL_TOOL


def test_flag_off_returns_none() -> None:
    """When the toggle is off, a `skill` call falls through unchanged."""
    response = _skill_call("alpha")
    decision = translate_control_tool(
        response,
        _assistant_message(response),
        toggles=ControlToggles(),  # all False, default
        skill_menu_names=_MENU,
    )
    assert decision is None


# ---------------------------------------------------------------------------
# Unit — success path
# ---------------------------------------------------------------------------


def test_translate_skill_success() -> None:
    response = _skill_call("alpha")
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is not None
    assert decision.patch.activate_skills == ["alpha"]
    # ack message
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    assert ack.role == "tool"
    assert len(ack.content) == 1
    block = ack.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == "sk"
    assert block.success is True
    assert block.error is None
    # ack bytes are fixed per the ADR
    assert block.output == (
        "Skill 'alpha' loaded; its instructions will appear in your "
        "context from the next turn."
    )


def test_translate_skill_success_carries_thinking() -> None:
    thinking = ThinkingBlock(text="reasoning", signature="sig-skill")
    tool = ToolUseBlock(
        call_id="sk", tool_name=SKILL_TOOL, arguments={"skill": "beta"}
    )
    response = LLMResponse(
        stop_reason="tool_use",
        content=[thinking, tool],
        usage=Usage(uncached=1, output=1),
        raw={"id": "sk"},
    )
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.assistant_thinking == (thinking,)
    assert decision.patch.activate_skills == ["beta"]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Unit — unknown name
# ---------------------------------------------------------------------------


def test_translate_skill_unknown_name() -> None:
    response = _skill_call("nope")
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None  # no state write
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    block = ack.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is False
    assert block.error is not None
    # lists the sorted available names so the model can retry
    assert block.output.startswith("unknown skill 'nope'; available:")
    # sorted order
    assert "alpha, beta, gamma" in block.output


def test_translate_skill_unknown_name_empty_menu() -> None:
    response = _skill_call("anything")
    decision = _translate(response, menu=frozenset())
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    block = decision.messages_after[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.output.endswith("available: (none)")


def test_translate_skill_missing_argument() -> None:
    response = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(call_id="sk", tool_name=SKILL_TOOL, arguments={})
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "sk"},
    )
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    block = decision.messages_after[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is False
    assert "skill must be a non-empty string" in block.output


# ---------------------------------------------------------------------------
# Unit — sole-call violation
# ---------------------------------------------------------------------------


def test_translate_skill_mixed_with_other_tool() -> None:
    response = _mixed_skill_and_other_call()
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    # two tool-result blocks (one per call_id), same error text
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    assert len(ack.content) == 2
    for b in ack.content:
        assert isinstance(b, ToolResultBlock)
        assert b.success is False
        assert b.output == "skill must be the only tool call in the turn"
    call_ids = sorted(b.call_id for b in ack.content)
    assert call_ids == ["s1", "sk"]


def test_translate_skill_two_skill_calls_is_sole_call_violation() -> None:
    """Two `skill` invocations in one turn is still a sole-call violation —
    only one activation per turn is allowed (model should retry with a
    single call, or make two turns)."""
    response = _two_skill_calls()
    decision = _translate(response)
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    assert len(decision.messages_after[0].content) == 2
    for b in decision.messages_after[0].content:
        assert isinstance(b, ToolResultBlock)
        assert b.success is False
        assert b.output == "skill must be the only tool call in the turn"


def test_translate_spawn_mixed_with_skill_recoverable_with_both_toggles() -> None:
    """When BOTH delegation and skill_invocation are on, a turn mixing
    `spawn_subagent` and `skill` must return a recoverable
    StatePatchDecision — NOT a FailDecision. All control
    tools share the sole-call philosophy of a recoverable ack so the
    task is not poisoned."""
    response = _mixed_skill_and_other_call()
    decision = translate_control_tool(
        response,
        _assistant_message(response),
        toggles=ControlToggles(delegation=True, skill_invocation=True),
        skill_menu_names=_MENU,
    )
    # Spawn branch is tried before skill in routing order — the mixed
    # batch is caught there and turned into an ack.
    assert isinstance(decision, StatePatchDecision)
    assert decision.patch is None
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    assert ack.role == "tool"
    assert len(ack.content) == 2
    call_ids = sorted(b.call_id for b in ack.content)
    assert call_ids == ["s1", "sk"]
    for b in ack.content:
        assert isinstance(b, ToolResultBlock)
        assert b.success is False
        assert b.error is not None
        assert "spawn_subagent cannot be mixed with other tool calls" in b.output


# ---------------------------------------------------------------------------
# Unit — duplicate (idempotent success)
# ---------------------------------------------------------------------------


def test_translate_skill_duplicate_name_same_ack() -> None:
    """Translating the same name twice produces byte-identical acks —
    idempotency is left to TaskStatePatch.apply's union merge."""
    first = _translate(_skill_call("gamma"))
    second = _translate(_skill_call("gamma"))
    assert isinstance(first, StatePatchDecision)
    assert isinstance(second, StatePatchDecision)
    # same patch (list equality, not identity)
    assert first.patch == second.patch
    assert first.patch.activate_skills == ["gamma"]  # type: ignore[union-attr]
    # byte-identical ack
    first_ack = first.messages_after[0].content[0]
    second_ack = second.messages_after[0].content[0]
    assert isinstance(first_ack, ToolResultBlock)
    assert isinstance(second_ack, ToolResultBlock)
    assert first_ack.output == second_ack.output
    assert first_ack.output == (
        "Skill 'gamma' loaded; its instructions will appear in your "
        "context from the next turn."
    )


# ---------------------------------------------------------------------------
# Engine-level — full chain through stub provider
# ---------------------------------------------------------------------------


def _make_ws_with_skill(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("foo\n")
    write_skill(ws, "alpha", description="the alpha skill")
    return ws


def _build_engine_for_tests(
    ws: Path,
    responses: list[LLMResponse],
    *,
    skill_invocation_enabled: bool = True,
    system_prompt: str = "you are helpful",
    pass_content_hashes: bool = True,
):
    """Build a fully-wired Engine from build_session_inputs using the
    same construction order the live runner uses (post the
    issue-07 generation switch: the generic ``content_hashes`` seam).
    Returns the engine, dispatcher, and task_id.

    ``pass_content_hashes=False`` simulates a host that didn't wire the
    resolver; in that case mid-loop content provenance must be absent
    and no errors raised.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.guards.budget import Budget
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )

    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)

    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt=system_prompt,
        allowed_tools=frozenset({"read_file"}),
        content_store=cs,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        skill_invocation_enabled=skill_invocation_enabled,
    )
    provider = FakeLLMProvider(responses=list(responses))
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs
    )
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(client),
        tools=inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=inputs.hooks,
        content_hashes=(inputs.content_hashes if pass_content_hashes else None),
    )
    return engine, disp, cs, log


def _run_to_terminal(engine, disp, task) -> None:
    """Run an engine task in a tight lease loop until terminal."""
    max_steps = 30
    for _ in range(max_steps):
        lease = disp.lease(worker_id="w")
        if lease is None:
            return
        task = engine.run_one_step(task, lease_id=lease.lease_id)
        if task.status in ("completed", "failed", "suspended"):
            return


def test_engine_skill_invocation_full_chain(tmp_path: Path) -> None:
    """Model calls `skill(alpha)` → patch lands → active_skills contains
    `alpha` → next compose semi-stable segment has body text →
    ContextPlan.selected_skills contains `alpha`."""
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws, [_skill_call("alpha"), _end("done")]
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    # 1. patch landed in active_skills
    folded = fold(log, cs, tid)
    assert "alpha" in folded.state.active_skills

    # 2. an ack tool-role message was appended (conversation well-formed)
    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs
    last_tool = tool_msgs[-1]
    assert len(last_tool.content) == 1
    block = last_tool.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is True
    assert block.output.startswith("Skill 'alpha' loaded")

    # 3. at least one ContextPlanComposed fired after the patch; the last
    #    one should carry the skill body in semi-stable and selected_skills.
    plan_events = [
        e for e in log.read(tid) if e.type == "ContextPlanComposed"
    ]
    assert len(plan_events) >= 2  # pre-turn + post-patch recompose
    last_payload = plan_events[-1].payload
    body = cs.get(last_payload.plan_ref)
    plan = from_canonical_bytes(body)
    assert isinstance(plan, ContextPlan)
    assert "alpha" in plan.selected_skills

    # 4. the composer, given the post-patch task, renders the skill body
    #    into the semi-stable segment.
    post_task = fold(log, cs, tid)
    view = engine._composer.compose(post_task)
    semi = view.segments[1]
    assert semi.name == "semi_stable"
    assert len(semi.content) >= 1
    skill_msg = semi.content[0]
    assert isinstance(skill_msg, Message)
    assert skill_msg.role == "user"
    skill_block = skill_msg.content[0]
    assert isinstance(skill_block, TextBlock)
    assert skill_block.text.startswith("Activated skill: alpha")
    assert "Body of the alpha skill." in skill_block.text


def test_engine_skill_invocation_no_tool_execution_events(tmp_path: Path) -> None:
    """The `skill` control tool must never reach ToolRuntime —
    no ToolCallStarted / ToolResultRecorded events are emitted."""
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws, [_skill_call("alpha"), _end("done")]
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    types = [e.type for e in log.read(task.task_id)]
    assert "TaskStatePatched" in types
    assert "ToolCallStarted" not in types
    assert "ToolResultRecorded" not in types


# ---------------------------------------------------------------------------
# Mid-loop content provenance (generic shape)
# ---------------------------------------------------------------------------


def test_engine_skill_invocation_emits_generic_provenance_before_patch(
    tmp_path: Path,
) -> None:
    """Mid-loop skill activation must emit ContextContentRecorded
    (kind=skill, policy=pinned) *before* TaskStatePatched so causal
    order matches the pre-loop helper's convention; the old
    SkillContentRecorded never appears in a new recording."""
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws, [_skill_call("alpha"), _end("done")]
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    events = list(log.read(task.task_id))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    assert "ContextContentRecorded" in types
    assert "TaskStatePatched" in types
    scr_idx = types.index("ContextContentRecorded")
    tsp_idx = types.index("TaskStatePatched")
    assert scr_idx < tsp_idx
    # Check payload shape
    scr = events[scr_idx]
    assert scr.payload.kind == "skill"
    assert scr.payload.name == "alpha"
    assert scr.payload.version == "1"
    assert len(scr.payload.content_hash) == 64  # sha256 hex
    assert scr.payload.policy == "pinned"


def test_engine_skill_invocation_duplicate_does_not_reemit(
    tmp_path: Path,
) -> None:
    """Duplicate activations of the same skill within one task must
    not re-emit SkillContentRecorded (per-task first-only)."""
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws,
        [
            _skill_call("alpha", call_id="a1"),
            _skill_call("alpha", call_id="a2"),
            _end("done"),
        ],
    )
    task = engine.create_task(goal="invoke twice", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    events = list(log.read(task.task_id))
    scr_count = sum(1 for e in events if e.type == "ContextContentRecorded")
    assert scr_count == 1
    # Both activations still land as patches (idempotent patching)
    tsp_count = sum(1 for e in events if e.type == "TaskStatePatched")
    assert tsp_count == 2


def test_engine_skill_invocation_no_resolver_no_event_no_crash(
    tmp_path: Path,
) -> None:
    """Host that doesn't wire content_hashes must not emit any content
    provenance and must not crash — byte shape matches recordings that
    predate provenance events."""
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws,
        [_skill_call("alpha"), _end("done")],
        pass_content_hashes=False,
    )
    task = engine.create_task(goal="invoke", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    events = list(log.read(task.task_id))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    assert "ContextContentRecorded" not in types
    # Activation still works
    assert "TaskStatePatched" in types
    post = fold(log, cs, task.task_id)
    assert "alpha" in post.state.active_skills


def test_engine_skill_invocation_unknown_skill_no_event_no_crash(
    tmp_path: Path,
) -> None:
    """Activating a name the resolver doesn't know must not emit
    SkillContentRecorded and must not crash. Verify will classify
    this as an advisory."""
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.guards.budget import Budget
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )

    ws = _make_ws_with_skill(tmp_path)
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)

    inputs = build_session_inputs(
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read_file"}),
        content_store=cs,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        skill_invocation_enabled=True,
    )
    provider = FakeLLMProvider(responses=[_skill_call("alpha"), _end("done")])
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
    # Build engine with a resolver that always returns None.
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(client),
        tools=inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=inputs.hooks,
        content_hashes=lambda _kind, _name: None,
    )
    task = engine.create_task(goal="invoke", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)

    events = list(log.read(task.task_id))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    assert "ContextContentRecorded" not in types
    assert "TaskStatePatched" in types
    post = fold(log, cs, task.task_id)
    assert "alpha" in post.state.active_skills


# ---------------------------------------------------------------------------
# Issue 05 — E2E: full chain (menu visible → order → ack → next-turn body →
# ContextContentRecorded) AND pre-loop + mid-loop coexist
# ---------------------------------------------------------------------------


def test_e2e_presets_flag_full_chain(
    tmp_path: Path,
) -> None:
    """Acceptance: wiring through the official presets
    and the noeta-agent product default (flag on).

    Covers the full chain end-to-end:
      1. Workspace has a skill → schema exposes the `skill` control tool
         with the correct name in the menu enum.
      2. Model orders `skill(bravo)` via the tool.
      3. Ack "loaded" message appended as a tool-role message.
      4. Next turn's semi-stable segment contains the skill body.
      5. ContextContentRecorded (kind=skill, policy=pinned) is in the
         event log (before the patch) — the post-cutover generic shape.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.policies.control_tools import SKILL_TOOL
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )
    from tests._session_inputs import build_code_replay_inputs
    from noeta.presets import official_specs

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "x.py").write_text("hello\n")
    write_skill(ws, "bravo", description="the bravo skill")

    # Use the official `main` preset spec so we exercise the preset flag
    # wiring rather than a free-form AgentSpec.
    main = official_specs()["main"]
    assert main.capabilities.skill_invocation is True, (
        "preset must have flag on for this E2E to exercise product wiring"
    )

    # -- Live construction (use the shared resume helper so live/resume
    #    build the same inputs; its default skill_invocation_enabled=True
    #    mirrors CodeSessionConfig default). ---------------------------
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)

    live_inputs = build_code_replay_inputs(
        workspace_dir=ws,
        agent=main,
        content_store=cs,
        model="stub-model",
    )
    # 1. schema exposes the `skill` tool with "bravo" in the menu.
    schema_names = {s["function"]["name"] for s in live_inputs.composer._control_action_schemas}
    assert SKILL_TOOL in schema_names, "flag on + skills present → skill tool visible"
    skill_schema = next(
        s for s in live_inputs.composer._control_action_schemas
        if s["function"]["name"] == SKILL_TOOL
    )
    enum = skill_schema["function"]["parameters"]["properties"]["skill"]["enum"]
    assert "bravo" in enum

    responses = [_skill_call("bravo"), _end("done")]
    provider = FakeLLMProvider(responses=list(responses))
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=live_inputs.composer,
        policy=live_inputs.policy_factory(client),
        tools=live_inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=live_inputs.hooks,
        content_hashes=live_inputs.content_hashes,
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    # 2. model ordered the skill → active_skills contains "bravo".
    folded = fold(log, cs, tid)
    assert "bravo" in folded.state.active_skills

    # 3. ack appended (tool-role "loaded" message).
    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs
    last_tool = tool_msgs[-1]
    block = last_tool.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is True
    assert "Skill 'bravo' loaded" in block.output

    # 4. next-turn semi-stable segment carries the skill body.
    view = engine._composer.compose(folded)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    joined = "\n".join(
        b.text for m in semi.content if isinstance(m, Message)
        for b in m.content if isinstance(b, TextBlock)
    )
    assert "Activated skill: bravo" in joined
    assert "Body of the bravo skill." in joined

    # 5. ContextContentRecorded present (mid-loop generic provenance).
    events = list(log.read(tid))
    types = [e.type for e in events]
    assert "SkillContentRecorded" not in types
    assert "ContextContentRecorded" in types
    scr = next(e for e in events if e.type == "ContextContentRecorded")
    assert scr.payload.kind == "skill"
    assert scr.payload.name == "bravo"
    assert scr.payload.version == "1"
    assert len(scr.payload.content_hash) == 64
    assert scr.payload.policy == "pinned"


def test_e2e_preloop_skill_coexists_with_midloop_skill(
    tmp_path: Path,
) -> None:
    """Acceptance: pre-loop static activation (skill A) and
    mid-loop model ordering (skill B) coexist. Both bodies appear in the
    semi-stable segment; active_skills contains both names; two
    ContextContentRecorded events are emitted; live and resume build the same inputs.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.skills import activate_skills
    from noeta.runtime.llm import RuntimeLLMClient
    from noeta.runtime.tool import ToolRuntime
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )
    from tests._session_inputs import build_code_replay_inputs
    from noeta.presets import official_specs

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    write_skill(ws, "alpha", description="pre-loaded")
    write_skill(ws, "bravo", description="ordered on demand")

    main = official_specs()["main"]

    # -- Live (homologous with replay, both via build_code_replay_inputs)
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    # lease_validator=None makes the EventLog skip lease_id validity checks, so
    # pre-loop activate_skills can use a synthetic lease_id (bypassing the
    # dispatcher state machine), while _run_to_terminal can still pull a real
    # lease from the dispatcher normally.
    log = InMemoryEventLog(lease_validator=None)
    wire_default_observers(log, disp)

    live_inputs = build_code_replay_inputs(
        workspace_dir=ws,
        agent=main,
        content_store=cs,
        model="stub-model",
    )

    responses = [_skill_call("bravo"), _end("done")]
    provider = FakeLLMProvider(responses=list(responses))
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=live_inputs.composer,
        policy=live_inputs.policy_factory(client),
        tools=live_inputs.tools,
        tool_runtime=ToolRuntime(event_log=log, content_store=cs),
        hooks=live_inputs.hooks,
        content_hashes=live_inputs.content_hashes,
    )
    task = engine.create_task(goal="coexistence", policy_name="react")
    disp.enqueue(task.task_id)

    # Pre-loop: runner activates "alpha" before the first turn, using the
    # same activate_skills helper the product host uses. A synthetic
    # lease_id is sufficient here (lease_id is only recorded as event
    # provenance, not validated against the dispatcher by activate_skills
    # or Engine.apply_state_patch).
    task = activate_skills(
        engine,
        task,
        skills=["alpha"],
        lease_id="lease-preloop-synthetic",
        skill_registry=live_inputs.skill_registry,
    )
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    # Both names active.
    folded = fold(log, cs, tid)
    assert "alpha" in folded.state.active_skills
    assert "bravo" in folded.state.active_skills

    # Two generic provenance events, one per skill; no legacy events.
    scrs = [e for e in log.read(tid) if e.type == "ContextContentRecorded"]
    assert {s.payload.name for s in scrs} == {"alpha", "bravo"}
    assert all(s.payload.kind == "skill" for s in scrs)
    assert not [
        e for e in log.read(tid) if e.type == "SkillContentRecorded"
    ]

    # Semi-stable segment carries BOTH bodies.
    view = engine._composer.compose(folded)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    joined = "\n".join(
        b.text for m in semi.content if isinstance(m, Message)
        for b in m.content if isinstance(b, TextBlock)
    )
    assert "Body of the alpha skill." in joined
    assert "Body of the bravo skill." in joined


# ---------------------------------------------------------------------------
# Issue 05 — product default: flag on iff workspace has skills
# ---------------------------------------------------------------------------


def test_product_flag_on_with_skills_grows_skill_tool(tmp_path: Path) -> None:
    """noeta-agent product default: skill_invocation is on by default; when the
    workspace has at least one skill the schema carries the skill tool; when no
    skills exist the schema is unchanged (no skill tool)."""
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.guards.budget import Budget
    from noeta.policies.control_tools import SKILL_TOOL
    from noeta.presets import official_specs
    from noeta.storage.memory import InMemoryContentStore

    # Mirror the product default. The deleted ``CodeSessionConfig`` carried the
    # ``skill_invocation_enabled=True`` default; the production ``SdkHost`` now
    # reads ``spec.capabilities.skill_invocation`` instead, so the ``main``
    # preset's capability is the canonical home of that default. A future
    # refactor that turns it off surfaces here.
    assert official_specs()["main"].capabilities.skill_invocation is True, (
        "the main preset must default skill_invocation on"
    )

    cs = InMemoryContentStore()

    # Case A: workspace with a skill → skill tool present.
    ws_with = tmp_path / "ws_with"
    ws_with.mkdir()
    write_skill(ws_with, "alpha", "desc")
    inputs_with = build_session_inputs(
        workspace_dir=ws_with,
        system_prompt="p",
        allowed_tools=frozenset(),
        content_store=cs,
        model="stub",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        skill_invocation_enabled=True,
    )
    names_with = {
        s["function"]["name"] for s in inputs_with.composer._control_action_schemas
    }
    assert SKILL_TOOL in names_with

    # Case B: workspace without skills → skill tool absent (zero schema drift
    # for skill-less workspaces).
    ws_empty = tmp_path / "ws_empty"
    ws_empty.mkdir()
    inputs_empty = build_session_inputs(
        workspace_dir=ws_empty,
        system_prompt="p",
        allowed_tools=frozenset(),
        content_store=cs,
        model="stub",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        skill_invocation_enabled=True,
    )
    names_empty = {
        s["function"]["name"] for s in inputs_empty.composer._control_action_schemas
    }
    assert SKILL_TOOL not in names_empty
