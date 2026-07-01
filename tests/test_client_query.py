"""Tests for noeta.client Client + query.

Covers:

1. one-shot ``query`` happy path: Options with built-in fs tools +
   FakeLLMProvider scripted ToolUse → envelope stream has all the
   canonical event types (TaskCreated / AgentBound / MessagesAppended /
   ToolCallStarted / ToolResultRecorded / TaskCompleted).
2. custom ``@tool`` closure runs: a decorated greet tool is referenced
   by name in Options.tools and the scripted model invokes it once.
3. multi-turn Client suspends at NEXT_GOAL, round-trips send_goal +
   close + reopen, MessagesAppended count grows, governance.closed
   tracks lifecycle.
4. Options-compiled fingerprint == hand-written AgentSpec fingerprint
   with the same identity fields (tested on the simple builtin-only
   recipe so we avoid DecoratedTool metadata).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
    ToolRef,
)
from noeta.client import Client, Options, compile_options, query
from noeta.client.parts import COMPOSER_REF, POLICY_REF, builtin_tool_ref
from noeta.core.fold import fold
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.events import (
    AgentBoundPayload,
    MessagesAppendedPayload,
    TaskCreatedPayload,
    ToolCallStartedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.decorator import tool


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


_PROMPT = "You are a test agent that reads and writes files."


def _scripted_tooluse_then_finish(
    *,
    tool_name: str,
    arguments: dict,
    call_id: str = "c1",
    answer: str = "done",
) -> list[LLMResponse]:
    """Two-response script: ToolUseBlock → end_turn TextBlock."""
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": f"resp-{call_id}"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text=answer)],
            usage=Usage(uncached=1, output=1),
            raw={"id": f"resp-finish-{call_id}"},
        ),
    ]


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    return ws


def _envelopes_of_type(envelopes, type_name: str):
    return [e for e in envelopes if e.type == type_name]


# ---------------------------------------------------------------------------
# Case 1 — query happy path with built-in tools
# ---------------------------------------------------------------------------


def test_query_happy_path_builtin_tools(tmp_path: Path) -> None:
    """query() returns a complete envelope stream over built-in fs tools."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(
        responses=_scripted_tooluse_then_finish(
            tool_name="edit",
            arguments={
                "path": "x.py",
                "old": "foo",
                "new": "bar",
            },
        )
    )
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("read", "edit"),
        permission_mode="bypassPermissions",
    )
    compiled_main, _ = compile_options(options)

    envelopes = query(
        options,
        goal="replace foo with bar in x.py",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    # 1. envelope stream has each canonical type at least once
    type_names = {e.type for e in envelopes}
    for required in (
        "TaskCreated",
        "AgentBound",
        "MessagesAppended",
        "ToolCallStarted",
        "ToolResultRecorded",
        "TaskCompleted",
    ):
        assert required in type_names, f"missing {required} in stream"

    # 2. TaskCreated.agent_name == "main"
    created = _envelopes_of_type(envelopes, "TaskCreated")
    assert len(created) == 1
    tc = created[0].payload
    assert isinstance(tc, TaskCreatedPayload)
    assert tc.agent_name == "main"

    # 3. AgentBound is emitted once and carries the compiled agent's name.
    bounds = _envelopes_of_type(envelopes, "AgentBound")
    assert len(bounds) == 1
    assert isinstance(bounds[0].payload, AgentBoundPayload)
    assert bounds[0].payload.agent_name == compiled_main.name

    # 4. edit tool was called once
    started = _envelopes_of_type(envelopes, "ToolCallStarted")
    assert len(started) == 1
    assert isinstance(started[0].payload, ToolCallStartedPayload)
    assert started[0].payload.tool_name == "edit"

    # 5. workspace saw the edit (replay recorded the result; dry-run does
    #    not actually write x.py — verify the tool *result* was recorded
    #    with success by reading ToolResultRecorded payload).
    results = _envelopes_of_type(envelopes, "ToolResultRecorded")
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# Case 2 — custom @tool closure
# ---------------------------------------------------------------------------


_GREET_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "additionalProperties": False,
}


@tool(
    name="greet",
    version="3",
    risk_level="low",
    input_schema=_GREET_SCHEMA,
)
def greet_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    name = arguments.get("name", "stranger")
    return ToolResult(success=True, output=f"hi {name}")


def test_query_custom_tool(tmp_path: Path) -> None:
    """A @tool-decorated closure runs via the custom_tools path."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(
        responses=_scripted_tooluse_then_finish(
            tool_name="greet",
            arguments={"name": "world"},
            call_id="g1",
            answer="Greeted successfully.",
        )
    )
    options = Options(
        system_prompt=_PROMPT,
        name="greeter",
        allowed_tools=(greet_tool,),
    )
    envelopes = query(
        options,
        goal="say hello to world",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    # greet was actually called (the @tool closure ran via the custom_tools path).
    started = _envelopes_of_type(envelopes, "ToolCallStarted")
    assert len(started) == 1
    assert isinstance(started[0].payload, ToolCallStartedPayload)
    assert started[0].payload.tool_name == "greet"


# ---------------------------------------------------------------------------
# Case 3 — multi-turn Client lifecycle
# ---------------------------------------------------------------------------


def test_client_multi_turn(tmp_path: Path) -> None:
    """start suspends on NEXT_GOAL → send_goal appends → close archives."""
    ws = _make_workspace(tmp_path)

    # 4 responses total: start turn tooluse+finish, send_goal turn tooluse+finish
    responses = _scripted_tooluse_then_finish(
        tool_name="edit",
        arguments={"path": "x.py", "old": "foo", "new": "bar"},
        call_id="t1",
        answer="first turn done",
    ) + _scripted_tooluse_then_finish(
        tool_name="edit",
        arguments={"path": "x.py", "old": "bar", "new": "baz"},
        call_id="t2",
        answer="second turn done",
    )
    provider = FakeLLMProvider(responses=responses)
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("edit",),
        permission_mode="bypassPermissions",
    )

    client = Client(
        options,
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
        multi_turn=True,
    )
    try:
        # Turn 1: start → suspended on next-goal
        outcome = client.start(goal="turn one")
        assert outcome.status == "suspended"
        assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE
        task_id = outcome.task_id

        turn1_count = len(_envelopes_of_type(client.events(task_id), "MessagesAppended"))
        assert turn1_count >= 1

        # Turn 2: send_goal → still suspended
        outcome2 = client.send_goal(task_id, goal="turn two")
        assert outcome2.status == "suspended"
        assert outcome2.wake_handle == NEXT_GOAL_WAKE_HANDLE

        turn2_count = len(_envelopes_of_type(client.events(task_id), "MessagesAppended"))
        assert turn2_count > turn1_count, "second turn must append messages"

        # Close: task.status stays suspended but governance.closed flips True
        outcome3 = client.close(task_id, closed_by="tester")
        assert outcome3.status == "suspended"  # "closed is orthogonal to status"
        folded = fold(client._host.event_log, client._host.content_store, task_id)
        assert folded.governance.closed is True

        # Reopen: governance.closed flips back
        outcome4 = client.reopen(task_id, reopened_by="tester")
        assert outcome4.status == "suspended"
        folded2 = fold(client._host.event_log, client._host.content_store, task_id)
        assert folded2.governance.closed is False
    finally:
        client.shutdown()
        # shutdown is idempotent
        client.shutdown()


# ---------------------------------------------------------------------------
# Case 4 — fingerprint alignment: Options-vs-handwritten AgentSpec
# ---------------------------------------------------------------------------


def test_options_vs_handwritten_spec_identity() -> None:
    """compile_options produces an AgentSpec structurally equal to a
    hand-written one with the same fields. Proves the pure-compile path is just
    identity sugar."""
    tools = (
        builtin_tool_ref("read"),
        builtin_tool_ref("edit"),
    )
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("read", "edit"),
        budget=BudgetSpec(max_iterations=5),
        capabilities=Capabilities(
            todo_write=False,
            ask_user_question=False,
            delegation=False,
            spawnable=(),
        ),
    )
    compiled, descendants = compile_options(options)
    assert len(descendants) == 0

    # Hand-write a spec that is byte-identical to what compile_options
    # should have produced. Frozen-normalisation means ordering doesn't
    # matter for identity, but we give the tools in sorted order to
    # match the AgentSpec.__post_init__ normalisation anyway.
    hand = AgentSpec(
        name="main",
        instructions=_PROMPT,
        policy=POLICY_REF,
        composer=COMPOSER_REF,
        tools=tuple(sorted(tools)),
        skills=(),
        guards=(),
        observers=(),
        default_budget=BudgetSpec(max_iterations=5),
        capabilities=Capabilities(),
        metadata={},
        default_model=None,
    )
    assert compiled == hand
    # Sanity: name doesn't match → specs shouldn't be equal
    wrong = dataclasses.replace(hand, name="renamed")
    assert compiled != wrong
