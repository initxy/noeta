"""The ``skill`` control tool activates a skill by translating into a state
patch instead of executing as a tool.

Guards the four translation paths (success, unknown name, sole-call violation,
duplicate) and the whole chain through the Engine — model call →
StatePatchDecision → ``TaskState.active_skills`` → rendered body →
``ContextPlan.selected_skills``. A break anywhere along it leaves the model
without the instructions it just asked for, with nothing failing loudly.
"""

from __future__ import annotations

from pathlib import Path

from tests._skill_fixtures import write_skill

from tests._session_inputs import default_factory_kwargs
from noeta.agent.spec import agent_activates
from noeta.core.fold import fold
from noeta.builtins.delegation.impl import translate_spawn_subagent
from noeta.policies.control_semantics import (
    ControlToolSpec,
    translate_control_tool,
)
from noeta.builtins.skills.impl import (
    SKILL_TOOL,
    load_workspace_skills,
    make_skill_translate,
)
# Deliberately the deep module rather than the package door: the parity
# assertion below pins the re-export against the definition.
from noeta.builtins.skills.impl.control_tool import (
    SKILL_TOOL as _SKILLS_SKILL_TOOL,
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
    # The mount's translate closure captures its own menu; the dispatcher takes
    # specs in routing order (here only skill).
    return translate_control_tool(
        response,
        _assistant_message(response),
        specs=(ControlToolSpec(SKILL_TOOL, make_skill_translate(menu)),),
    )


# ---------------------------------------------------------------------------
# Sanity — constant re-export parity and schema visibility
# ---------------------------------------------------------------------------


def test_skill_tool_constant_parity() -> None:
    assert SKILL_TOOL == "skill"
    assert _SKILLS_SKILL_TOOL == SKILL_TOOL


def test_flag_off_returns_none() -> None:
    """Mounting IS enablement: a tool that contributes no spec routes nothing,
    so the call falls through untouched rather than erroring."""
    response = _skill_call("alpha")
    decision = translate_control_tool(
        response,
        _assistant_message(response),
        specs=(),
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
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    assert ack.role == "tool"
    assert len(ack.content) == 1
    block = ack.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == "sk"
    assert block.success is True
    assert block.error is None
    # The ack text is contractual: byte-stable so the model can rely on it.
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
    assert decision.patch is None
    assert len(decision.messages_after) == 1
    ack = decision.messages_after[0]
    block = ack.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is False
    assert block.error is not None
    # The ack lists the available names, sorted, so the model can retry
    # without a second round trip.
    assert block.output.startswith("unknown skill 'nope'; available:")
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
    # One tool-result block per call_id, or the provider rejects the next
    # request for an unanswered tool call.
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
    """Two `skill` calls in one turn is a sole-call violation too: one
    activation per turn, so ordering is unambiguous."""
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
    """A turn mixing `spawn_subagent` and `skill` yields a recoverable
    StatePatchDecision, never a FailDecision: every control tool answers a
    sole-call violation with an ack the model can retry from, so one bad
    batch cannot poison the Task."""
    response = _mixed_skill_and_other_call()
    decision = translate_control_tool(
        response,
        _assistant_message(response),
        # Routing order mirrors the mount loop: spawn is offered the batch
        # first, so its error text is the one the model sees.
        specs=(
            ControlToolSpec("spawn_subagent", translate_spawn_subagent),
            ControlToolSpec(SKILL_TOOL, make_skill_translate(_MENU)),
        ),
    )
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
    """Translation stays stateless: the same name twice yields byte-identical
    acks, and deduplication is left to TaskStatePatch.apply's union merge."""
    first = _translate(_skill_call("gamma"))
    second = _translate(_skill_call("gamma"))
    assert isinstance(first, StatePatchDecision)
    assert isinstance(second, StatePatchDecision)
    assert first.patch == second.patch
    assert first.patch.activate_skills == ["gamma"]  # type: ignore[union-attr]
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
    """Wire an Engine from ``build_session_inputs`` the way a host does.

    ``pass_content_hashes=False`` simulates a host that never wired the
    resolver: content provenance must then be absent, without raising.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.runtime.governance import Budget
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
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt=system_prompt,
        allowed_tools=frozenset({"read_file"}),
        content_store=cs,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": skill_invocation_enabled},
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
    `alpha` → the body renders anchored in the dynamic suffix → the plan's
    selected_skills contains `alpha`.

    Anchoring matters: a mid-task activation must leave the head segments
    byte-identical so the provider's prompt cache survives the turn
    (docs/adr/anchored-content-placement.md).
    """
    ws = _make_ws_with_skill(tmp_path)
    engine, disp, cs, log = _build_engine_for_tests(
        ws, [_skill_call("alpha"), _end("done")]
    )
    task = engine.create_task(goal="invoke a skill", policy_name="react")
    disp.enqueue(task.task_id)
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    folded = fold(log, cs, tid)
    assert "alpha" in folded.state.active_skills

    # The tool-role ack keeps the conversation well-formed: every tool_use
    # block the model emitted has a matching result.
    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs
    last_tool = tool_msgs[-1]
    assert len(last_tool.content) == 1
    block = last_tool.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is True
    assert block.output.startswith("Skill 'alpha' loaded")

    plan_events = [
        e for e in log.read(tid) if e.type == "ContextPlanComposed"
    ]
    assert len(plan_events) >= 2  # pre-turn + post-patch recompose
    last_payload = plan_events[-1].payload
    body = cs.get(last_payload.plan_ref)
    plan = from_canonical_bytes(body)
    assert isinstance(plan, ContextPlan)
    assert "alpha" in plan.selected_skills

    # Composing the post-patch Task puts the body in the dynamic suffix and
    # leaves semi_stable empty — the head segments must not move mid-task.
    post_task = fold(log, cs, tid)
    view = engine._composer.compose(post_task)
    semi = view.segments[1]
    assert semi.name == "semi_stable"
    assert semi.content == []
    dynamic = view.segments[2]
    assert dynamic.name == "dynamic_suffix"
    skill_msgs = [
        m
        for m in dynamic.content
        if m.role == "user"
        and m.content
        and isinstance(m.content[0], TextBlock)
        and m.content[0].text.startswith("Activated skill: alpha")
    ]
    assert len(skill_msgs) == 1
    skill_block = skill_msgs[0].content[0]
    assert "Body of the alpha skill." in skill_block.text
    # The body renders AFTER the activating turn's tool-role ack, never
    # between an assistant tool_use and its results.
    idx_skill = dynamic.content.index(skill_msgs[0])
    idx_tool = max(
        i for i, m in enumerate(dynamic.content) if m.role == "tool"
    )
    assert idx_skill > 0
    assert dynamic.content[idx_skill - 1].role != "assistant" or not any(
        isinstance(b, ToolUseBlock) for b in dynamic.content[idx_skill - 1].content
    )
    assert idx_skill != idx_tool


def test_engine_skill_invocation_no_tool_execution_events(tmp_path: Path) -> None:
    """The `skill` control tool must never reach ToolRuntime: it carries no
    permission check or audit trail of its own, so an execution path would be
    an ungoverned side door."""
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
    """Provenance is recorded through the generic ContextContentRecorded
    (kind=skill, policy=pinned), and lands *before* TaskStatePatched — a
    reader folding the log must see what the content was before it sees the
    state that depends on it."""
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
    scr = events[scr_idx]
    assert scr.payload.kind == "skill"
    assert scr.payload.name == "alpha"
    assert scr.payload.version == "1"
    assert len(scr.payload.content_hash) == 64  # sha256 hex
    assert scr.payload.policy == "pinned"


def test_engine_skill_invocation_duplicate_does_not_reemit(
    tmp_path: Path,
) -> None:
    """Provenance is recorded once per Task per content item, so repeated
    activations of the same skill do not pad the log with identical
    ContextContentRecorded events."""
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
    # Suppressing the provenance event must not suppress the patch itself.
    tsp_count = sum(1 for e in events if e.type == "TaskStatePatched")
    assert tsp_count == 2


def test_engine_skill_invocation_no_resolver_no_event_no_crash(
    tmp_path: Path,
) -> None:
    """``content_hashes`` is optional wiring: a host that leaves it out gets
    no provenance events, and activation still works rather than crashing."""
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
    assert "TaskStatePatched" in types
    post = fold(log, cs, task.task_id)
    assert "alpha" in post.state.active_skills


def test_engine_skill_invocation_unknown_skill_no_event_no_crash(
    tmp_path: Path,
) -> None:
    """A resolver that knows no names is a silent no-op for provenance: the
    activation itself must not depend on the fingerprint being resolvable."""
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.runtime.governance import Budget
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
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read_file"}),
        content_store=cs,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": True},
    )
    provider = FakeLLMProvider(responses=[_skill_call("alpha"), _end("done")])
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)
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
# End-to-end: menu visible → model orders → ack → body in context → provenance
# ---------------------------------------------------------------------------


def test_e2e_presets_flag_full_chain(
    tmp_path: Path,
) -> None:
    """The same chain, wired through the official presets rather than a
    hand-built AgentSpec — so preset drift that silently drops the skill
    capability is caught here and not only in a host.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.builtins.skills.impl import SKILL_TOOL
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

    main = official_specs()["main"]
    assert agent_activates(main, "skill_invocation") is True, (
        "preset must activate skill_invocation for this chain to be exercised"
    )

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

    folded = fold(log, cs, tid)
    assert "bravo" in folded.state.active_skills

    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs
    last_tool = tool_msgs[-1]
    block = last_tool.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.success is True
    assert "Skill 'bravo' loaded" in block.output

    # A mid-task activation anchors into the dynamic suffix and leaves
    # semi_stable untouched (docs/adr/anchored-content-placement.md).
    view = engine._composer.compose(folded)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    assert semi.content == []
    dynamic = next(s for s in view.segments if s.name == "dynamic_suffix")
    joined = "\n".join(
        b.text for m in dynamic.content if isinstance(m, Message)
        for b in m.content if isinstance(b, TextBlock)
    )
    assert "Activated skill: bravo" in joined
    assert "Body of the bravo skill." in joined

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
    """A skill activated before the first turn and one the model orders
    mid-task coexist: both names stay active and both bodies reach the
    prompt, but they land in different segments — the pre-loop one keeps its
    semi_stable seat while the mid-task one anchors in the dynamic suffix.
    """
    from noeta.core.engine import Engine
    from noeta.core.wiring import wire_default_observers
    from noeta.builtins.skills.impl import activate_skills
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

    # Activate "alpha" before the first turn through the same helper a host
    # calls. A synthetic lease_id suffices: neither activate_skills nor
    # Engine.apply_state_patch validates it against the dispatcher — it is
    # recorded as event provenance only.
    task = activate_skills(
        engine,
        task,
        skills=["alpha"],
        lease_id="lease-preloop-synthetic",
        skill_registry=load_workspace_skills(ws),
    )
    _run_to_terminal(engine, disp, task)
    tid = task.task_id

    folded = fold(log, cs, tid)
    assert "alpha" in folded.state.active_skills
    assert "bravo" in folded.state.active_skills

    # Both activation paths report through the same generic provenance event.
    scrs = [e for e in log.read(tid) if e.type == "ContextContentRecorded"]
    assert {s.payload.name for s in scrs} == {"alpha", "bravo"}
    assert all(s.payload.kind == "skill" for s in scrs)
    assert not [
        e for e in log.read(tid) if e.type == "SkillContentRecorded"
    ]

    # Placement splits by anchor (docs/adr/anchored-content-placement.md):
    # "alpha" was there before the first turn so it keeps its semi_stable
    # seat; "bravo" arrived mid-task so it anchors in the dynamic suffix.
    view = engine._composer.compose(folded)
    semi = next(s for s in view.segments if s.name == "semi_stable")
    semi_joined = "\n".join(
        b.text for m in semi.content if isinstance(m, Message)
        for b in m.content if isinstance(b, TextBlock)
    )
    assert "Body of the alpha skill." in semi_joined
    assert "Body of the bravo skill." not in semi_joined
    dynamic = next(s for s in view.segments if s.name == "dynamic_suffix")
    dyn_joined = "\n".join(
        b.text for m in dynamic.content if isinstance(m, Message)
        for b in m.content if isinstance(b, TextBlock)
    )
    assert "Body of the bravo skill." in dyn_joined


# ---------------------------------------------------------------------------
# Default: the skill tool appears iff the workspace actually has skills
# ---------------------------------------------------------------------------


def test_product_flag_on_with_skills_grows_skill_tool(tmp_path: Path) -> None:
    """With skill_invocation activated, the schema grows the skill tool only
    when the workspace holds a skill — a skill-less workspace sees zero schema
    drift, so users who never write a SKILL.md pay nothing."""
    from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
    from noeta.runtime.governance import Budget
    from noeta.builtins.skills.impl import SKILL_TOOL
    from noeta.presets import official_specs
    from noeta.storage.memory import InMemoryContentStore

    # ``SdkHost`` reads the spec's ``"skill_invocation"`` activation, so the
    # ``main`` preset is the single home of this default; a change that turns
    # it off surfaces here rather than in a host's behaviour.
    assert agent_activates(official_specs()["main"], "skill_invocation") is True, (
        "the main preset must default skill_invocation on"
    )

    cs = InMemoryContentStore()

    # Case A: workspace with a skill → skill tool present.
    ws_with = tmp_path / "ws_with"
    ws_with.mkdir()
    write_skill(ws_with, "alpha", "desc")
    inputs_with = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws_with,
        system_prompt="p",
        allowed_tools=frozenset(),
        content_store=cs,
        model="stub",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": True},
    )
    names_with = {
        s["function"]["name"] for s in inputs_with.composer._control_action_schemas
    }
    assert SKILL_TOOL in names_with

    # Case B: workspace without skills → skill tool absent.
    ws_empty = tmp_path / "ws_empty"
    ws_empty.mkdir()
    inputs_empty = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws_empty,
        system_prompt="p",
        allowed_tools=frozenset(),
        content_store=cs,
        model="stub",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": True},
    )
    names_empty = {
        s["function"]["name"] for s in inputs_empty.composer._control_action_schemas
    }
    assert SKILL_TOOL not in names_empty
