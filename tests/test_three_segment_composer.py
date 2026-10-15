"""ThreeSegmentComposer behavior tests.

Verifies the deterministic three-segment compose contract (a stable
prompt prefix the cross-host prompt cache depends on). Each test exercises one
observable behavior.
"""

from __future__ import annotations

from typing import Any


from noeta.client.parts import default_reminder_specs
from noeta.context.composer import RenderedContent, ThreeSegmentComposer
from noeta.context.reminders import ReminderRegistry
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.context_plan import ContextPlan
from noeta.protocols.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.storage.memory import InMemoryContentStore


class _FakeTool:
    """Minimal Tool stub for composer tests."""

    def __init__(
        self,
        name: str,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = input_schema or {
            "type": "object",
            "additionalProperties": True,
        }

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:  # pragma: no cover - never invoked by composer
        raise NotImplementedError


def _task_with(messages: list[Message], *, active_skills: list[str] | None = None) -> Task:
    task = Task(task_id="t-1", state=TaskState())
    if active_skills:
        # Activate through the patch sugar (the fold-derived shape): it
        # mirrors into the generic activation map the composer reads
        # post the issue-07 generation switch.
        TaskStatePatch(activate_skills=list(active_skills)).apply(task.state)
    task.runtime.messages.extend(messages)
    return task


def _composer(
    *,
    system_prompt: str = "you are a helpful agent",
    tools: dict[str, Tool] | None = None,
    skill_renderer: Any = None,
    content_store: InMemoryContentStore | None = None,
) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt=system_prompt,
        tools=tools if tools is not None else {"echo": _FakeTool("echo")},
        # Don't ``or`` here: ``InMemoryContentStore.__len__`` makes an
        # empty store falsy, which would silently swap in a fresh store
        # and lose the body the test wrote.
        content_store=content_store if content_store is not None else InMemoryContentStore(),
        skill_renderer=skill_renderer,
        # A bare composer has no reminders; these behavior
        # tests cover the built-in three, so inject them the way the builder
        # does (loader-resolved specs).
        reminders=ReminderRegistry(default_reminder_specs()),
    )


# ---------------------------------------------------------------------------
# Tracer bullet: shape
# ---------------------------------------------------------------------------


def test_compose_returns_three_named_segments_in_order() -> None:
    composer = _composer()
    task = _task_with([Message(role="user", content=[TextBlock(text="hi")])])

    view = composer.compose(task)

    assert len(view.segments) == 3
    assert view.segments[0].name == "stable_prefix"
    assert view.segments[1].name == "semi_stable"
    assert view.segments[2].name == "dynamic_suffix"


# ---------------------------------------------------------------------------
# Determinism + segment hashes
# ---------------------------------------------------------------------------


def test_compose_is_deterministic_across_calls_with_same_inputs() -> None:
    composer = _composer()
    task = _task_with([Message(role="user", content=[TextBlock(text="hi")])])

    first = composer.compose(task)
    second = composer.compose(task)

    assert [s.segment_hash for s in first.segments] == [
        s.segment_hash for s in second.segments
    ]
    assert first.plan_ref == second.plan_ref


def test_changing_system_prompt_changes_only_stable_prefix_hash() -> None:
    base = _composer(system_prompt="prompt A")
    other = _composer(system_prompt="prompt B")
    task = _task_with([Message(role="user", content=[TextBlock(text="hi")])])

    v1 = base.compose(task)
    v2 = other.compose(task)

    assert v1.segments[0].segment_hash != v2.segments[0].segment_hash
    assert v1.segments[1].segment_hash == v2.segments[1].segment_hash
    assert v1.segments[2].segment_hash == v2.segments[2].segment_hash


def test_changing_tools_changes_stable_prefix_hash() -> None:
    base = _composer(tools={"echo": _FakeTool("echo")})
    other = _composer(
        tools={
            "echo": _FakeTool("echo"),
            "lookup": _FakeTool("lookup"),
        }
    )
    task = _task_with([Message(role="user", content=[TextBlock(text="hi")])])

    h1 = base.compose(task).segments[0].segment_hash
    h2 = other.compose(task).segments[0].segment_hash
    assert h1 != h2


def test_changing_active_skills_changes_semi_stable_hash() -> None:
    renderer = lambda skills, resolve=None: RenderedContent(  # noqa: E731
        messages=[
            Message(role="user", content=[TextBlock(text=f"skill:{s}")])
            for s in skills
        ],
        selected_skills=list(skills),
    )
    composer = _composer(skill_renderer=renderer)

    task_a = _task_with(
        [Message(role="user", content=[TextBlock(text="hi")])],
        active_skills=[],
    )
    task_b = _task_with(
        [Message(role="user", content=[TextBlock(text="hi")])],
        active_skills=["s1"],
    )

    v_a = composer.compose(task_a)
    v_b = composer.compose(task_b)

    assert v_a.segments[0].segment_hash == v_b.segments[0].segment_hash
    assert v_a.segments[1].segment_hash != v_b.segments[1].segment_hash


def test_appending_dynamic_message_changes_only_dynamic_suffix_hash() -> None:
    composer = _composer()
    msgs = [Message(role="user", content=[TextBlock(text="hi")])]
    task = _task_with(msgs)

    before = composer.compose(task)
    task.runtime.messages.append(
        Message(role="assistant", content=[TextBlock(text="ok")])
    )
    after = composer.compose(task)

    assert before.segments[0].segment_hash == after.segments[0].segment_hash
    assert before.segments[1].segment_hash == after.segments[1].segment_hash
    assert before.segments[2].segment_hash != after.segments[2].segment_hash


# ---------------------------------------------------------------------------
# provider_tool_schemas field
# ---------------------------------------------------------------------------


def test_provider_tool_schemas_is_populated_from_tool_input_schemas() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    composer = _composer(tools={"echo": _FakeTool("echo", input_schema=schema)})
    task = _task_with([])

    view = composer.compose(task)

    assert any(
        item.get("function", {}).get("name") == "echo"
        and item.get("function", {}).get("parameters") == schema
        for item in view.provider_tool_schemas
    )


# ---------------------------------------------------------------------------
# ContextPlan body
# ---------------------------------------------------------------------------


def test_plan_ref_body_deserializes_to_context_plan_with_expected_fields() -> None:
    store = InMemoryContentStore()
    renderer = lambda skills, resolve=None: RenderedContent(  # noqa: E731
        messages=[
            Message(role="user", content=[TextBlock(text=f"skill:{s}")])
            for s in skills
        ],
        selected_skills=list(skills),
    )
    composer = _composer(content_store=store, skill_renderer=renderer)
    task = _task_with(
        [Message(role="user", content=[TextBlock(text="hi")])],
        active_skills=["s1"],
    )

    view = composer.compose(task)

    assert view.plan_ref is not None
    body = store.get(view.plan_ref)
    plan = from_canonical_bytes(body)
    assert isinstance(plan, ContextPlan)
    assert plan.composer_version == "three_segment.v5"
    assert set(plan.segment_hashes.keys()) == {
        "stable_prefix",
        "semi_stable",
        "dynamic_suffix",
    }
    assert plan.segment_hashes["stable_prefix"] == view.segments[0].segment_hash
    assert plan.selected_skills == ["s1"]


def test_dynamic_suffix_content_is_task_runtime_messages_verbatim() -> None:
    composer = _composer()
    msgs = [
        Message(role="user", content=[TextBlock(text="hi")]),
        Message(role="assistant", content=[TextBlock(text="hello")]),
    ]
    task = _task_with(msgs)

    view = composer.compose(task)

    assert view.segments[2].content == msgs


def test_iter_messages_returns_semi_stable_and_dynamic_suffix() -> None:
    renderer = lambda skills, resolve=None: RenderedContent(  # noqa: E731
        messages=[
            Message(role="user", content=[TextBlock(text=f"skill:{s}")])
            for s in skills
        ],
        selected_skills=list(skills),
    )
    composer = _composer(skill_renderer=renderer)
    user_msg = Message(role="user", content=[TextBlock(text="hi")])
    task = _task_with([user_msg], active_skills=["s1"])

    view = composer.compose(task)

    history = view.iter_messages()
    assert history[-1] == user_msg
    assert any(
        isinstance(m.content[0], TextBlock)
        and m.content[0].text.startswith("skill:s1")
        for m in history
    )


# ---------------------------------------------------------------------------
# Default skill_renderer
# ---------------------------------------------------------------------------


def test_default_content_renderer_yields_empty_semi_stable() -> None:
    composer = _composer()  # no renderer
    task = _task_with([], active_skills=["s1"])

    view = composer.compose(task)
    assert view.segments[1].content == []


# ---------------------------------------------------------------------------
# Issue C — control_action_schemas (spawn_subagent) in View + stable hash
# ---------------------------------------------------------------------------


def test_control_action_schemas_default_off_leaves_schema_and_hash_unchanged() -> None:
    """Disabling delegation (no control schemas) must leave the tools
    schema + stable hash byte-identical to before the seam existed."""
    base = _composer()
    cs = InMemoryContentStore()
    with_none = ThreeSegmentComposer(
        system_prompt="you are a helpful agent",
        tools={"echo": _FakeTool("echo")},
        content_store=cs,
        control_action_schemas=None,
    )
    task = _task_with([])
    v0 = base.compose(task)
    v1 = with_none.compose(task)
    assert v0.provider_tool_schemas == v1.provider_tool_schemas
    assert v0.segments[0].segment_hash == v1.segments[0].segment_hash


def test_control_action_schemas_appear_in_view_and_rotate_stable_hash() -> None:
    """A control schema is visible to the provider (in View.provider_tool_schemas)
    and rotates the stable_prefix hash (so the prompt tool surface is
    reflected)."""
    extra = {"type": "function", "function": {"name": "spawn_subagent", "parameters": {}}}
    plain = _composer()
    cs = InMemoryContentStore()
    deleg = ThreeSegmentComposer(
        system_prompt="you are a helpful agent",
        tools={"echo": _FakeTool("echo")},
        content_store=cs,
        control_action_schemas=[extra],
    )
    task = _task_with([])
    v_plain = plain.compose(task)
    v_deleg = deleg.compose(task)
    names = [t["function"]["name"] for t in v_deleg.provider_tool_schemas]
    assert "spawn_subagent" in names
    assert names[-1] == "spawn_subagent"  # deterministic: control after real
    assert (
        v_deleg.segments[0].segment_hash != v_plain.segments[0].segment_hash
    )


# ---------------------------------------------------------------------------
# Extended thinking re-attach (Slice C)
# ---------------------------------------------------------------------------


def test_compose_reattaches_thinking_to_assistant_turn_by_call_id() -> None:
    """``ContextState.thinking_by_call_id`` carries the thinking blocks that
    ``_strip_thinking`` removed from the history, keyed by the assistant
    turn's first ``tool_use`` ``call_id``. Compose re-attaches them at the
    head of that assistant message in the ``dynamic_suffix`` so an Anthropic
    continuation request can replay the signature.

    Provider-neutral: the composer always re-attaches; each
    adapter gates the OUTBOUND reasoning (Anthropic passes it through,
    OpenAI drops it per ``reasoning_continuation``). The thinking never
    lives in ``runtime.messages`` (that would change a resume's rebuilt history) —
    it is re-attached only into the transient View.
    """
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(call_id="c1", tool_name="echo", arguments={})],
    )
    task = _task_with(
        [
            Message(role="user", content=[TextBlock(text="hi")]),
            assistant,
            Message(
                role="tool",
                content=[ToolResultBlock(call_id="c1", output="ok", success=True)],
            ),
        ]
    )
    thinking = ThinkingBlock(text="let me think", signature="sig-1")
    task.context.thinking_by_call_id = {"c1": [thinking]}

    view = _composer().compose(task)

    dynamic = view.segments[2].content
    restored = [m for m in dynamic if m.role == "assistant"]
    assert len(restored) == 1
    # thinking is prepended, ahead of the tool_use it preceded.
    assert restored[0].content[0] == thinking
    assert any(
        isinstance(b, ToolUseBlock) and b.call_id == "c1"
        for b in restored[0].content
    )


def test_compose_without_thinking_slice_leaves_history_untouched() -> None:
    """Empty ``thinking_by_call_id`` (the default / OpenAI / old recording)
    re-attaches nothing — the dynamic assistant turn is byte-identical to
    the stripped history, so old recordings fold byte-equal."""
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(call_id="c1", tool_name="echo", arguments={})],
    )
    task = _task_with(
        [
            Message(role="user", content=[TextBlock(text="hi")]),
            assistant,
        ]
    )

    view = _composer().compose(task)

    dynamic = view.segments[2].content
    restored = [m for m in dynamic if m.role == "assistant"]
    assert restored[0].content == [
        ToolUseBlock(call_id="c1", tool_name="echo", arguments={})
    ]


# ---------------------------------------------------------------------------
# Layer-3 fan-out nudge — just-in-time concurrency reminder
# ---------------------------------------------------------------------------

_SPAWN_SCHEMA = {
    "type": "function",
    "function": {"name": "Task", "parameters": {}},
}


def _delegating_composer() -> ThreeSegmentComposer:
    """A composer that offers delegation (``Task`` in the tool
    surface), the precondition for the concurrency reminder."""
    return ThreeSegmentComposer(
        system_prompt="you are a helpful agent",
        tools={"echo": _FakeTool("echo")},
        content_store=InMemoryContentStore(),
        control_action_schemas=[_SPAWN_SCHEMA],
        reminders=ReminderRegistry(default_reminder_specs()),
    )


def _system_reminders(view: Any) -> list[str]:
    """The text of every ``origin="system"`` reminder in the dynamic_suffix."""
    return [
        m.content[0].text
        for m in view.segments[2].content
        if m.origin == "system" and m.content and isinstance(m.content[0], TextBlock)
    ]


def test_offered_delegation_appends_no_reminder() -> None:
    """Delegation being offered and unused appends NOTHING.

    ``delegation-nudge`` used to append a turn here on every step until the
    first spawn, restating main rule 10 and the ``Task`` description. It was
    removed; offering delegation must cost no tokens of its own.
    """
    view = _delegating_composer().compose(
        _task_with([Message(role="user", content=[TextBlock(text="hi")])])
    )
    assert _system_reminders(view) == []


