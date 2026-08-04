"""The two entry points users call: one-shot ``query`` and multi-turn ``Client``.

The sharpest case here is a spilled answer. ``query`` tears down its temporary
``Client`` and ``ContentStore`` before returning, so ``QueryResult`` must have
materialised the answer already — otherwise the caller is left holding a
``ContentRef`` nothing can resolve. The multi-turn cases pin that closing a
conversation is orthogonal to task status: ``governance.closed`` flips while the
task stays suspended and resumable.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
)
from noeta.client import (
    Client,
    Options,
    QueryFailedError,
    QueryResult,
    Result,
    compile_options,
    query,
)
from noeta.client.parts import COMPOSER_REF, POLICY_REF, builtin_tool_ref
from noeta.core.fold import fold
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.events import (
    AgentBoundPayload,
    TaskCompletedPayload,
    TaskCreatedPayload,
    TaskFailedPayload,
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
# query happy path with built-in tools
# ---------------------------------------------------------------------------


def test_query_happy_path_builtin_tools(tmp_path: Path) -> None:
    """query() returns a complete envelope stream over built-in fs tools."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(
        responses=_scripted_tooluse_then_finish(
            tool_name="Edit",
            arguments={
                "path": "x.py",
                "old_string": "foo",
                "new_string": "bar",
            },
        )
    )
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("Read", "Edit"),
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

    created = _envelopes_of_type(envelopes, "TaskCreated")
    assert len(created) == 1
    tc = created[0].payload
    assert isinstance(tc, TaskCreatedPayload)
    assert tc.agent_name == "main"

    bounds = _envelopes_of_type(envelopes, "AgentBound")
    assert len(bounds) == 1
    assert isinstance(bounds[0].payload, AgentBoundPayload)
    assert bounds[0].payload.agent_name == compiled_main.name

    started = _envelopes_of_type(envelopes, "ToolCallStarted")
    assert len(started) == 1
    assert isinstance(started[0].payload, ToolCallStartedPayload)
    assert started[0].payload.tool_name == "Edit"

    # Dry-run never writes x.py, so the recorded result is the only evidence
    # the edit ran.
    results = _envelopes_of_type(envelopes, "ToolResultRecorded")
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# custom @tool closure
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

    # Proves the decorated closure was mounted through the custom_tools path.
    started = _envelopes_of_type(envelopes, "ToolCallStarted")
    assert len(started) == 1
    assert isinstance(started[0].payload, ToolCallStartedPayload)
    assert started[0].payload.tool_name == "greet"


# ---------------------------------------------------------------------------
# multi-turn Client lifecycle
# ---------------------------------------------------------------------------


def test_client_multi_turn(tmp_path: Path) -> None:
    """start suspends on NEXT_GOAL → send_goal appends → close archives."""
    ws = _make_workspace(tmp_path)

    # 4 responses total: start turn tooluse+finish, send_goal turn tooluse+finish
    responses = _scripted_tooluse_then_finish(
        tool_name="Edit",
        arguments={"file_path": "x.py", "old_string": "foo", "new_string": "bar"},
        call_id="t1",
        answer="first turn done",
    ) + _scripted_tooluse_then_finish(
        tool_name="Edit",
        arguments={"file_path": "x.py", "old_string": "bar", "new_string": "baz"},
        call_id="t2",
        answer="second turn done",
    )
    provider = FakeLLMProvider(responses=responses)
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Edit",),
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
        outcome = client.start(goal="turn one")
        assert outcome.status == "suspended"
        assert outcome.wake_handle == NEXT_GOAL_WAKE_HANDLE
        task_id = outcome.task_id

        turn1_count = len(_envelopes_of_type(client.events(task_id), "MessagesAppended"))
        assert turn1_count >= 1

        outcome2 = client.send_goal(task_id, goal="turn two")
        assert outcome2.status == "suspended"
        assert outcome2.wake_handle == NEXT_GOAL_WAKE_HANDLE

        turn2_count = len(_envelopes_of_type(client.events(task_id), "MessagesAppended"))
        assert turn2_count > turn1_count, "second turn must append messages"

        # Closing is orthogonal to status: the task stays suspended.
        outcome3 = client.close(task_id, closed_by="tester")
        assert outcome3.status == "suspended"
        folded = fold(client._host.event_log, client._host.content_store, task_id)
        assert folded.governance.closed is True

        outcome4 = client.reopen(task_id, reopened_by="tester")
        assert outcome4.status == "suspended"
        folded2 = fold(client._host.event_log, client._host.content_store, task_id)
        assert folded2.governance.closed is False
    finally:
        client.shutdown()
        # shutdown is idempotent
        client.shutdown()


# ---------------------------------------------------------------------------
# Options-compiled spec vs hand-written AgentSpec
# ---------------------------------------------------------------------------


def test_options_vs_handwritten_spec_identity() -> None:
    """compile_options produces an AgentSpec structurally equal to a
    hand-written one with the same fields. Proves the pure-compile path is just
    identity sugar."""
    tools = (
        builtin_tool_ref("Read"),
        builtin_tool_ref("Edit"),
    )
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("Read", "Edit"),
        budget=BudgetSpec(max_iterations=5),
    )
    compiled, descendants = compile_options(options)
    assert len(descendants) == 0

    # AgentSpec.__post_init__ normalises ordering, so tuple order does not
    # affect identity; the tools are pre-sorted here only to read the same way.
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
        plugins=("fs", "web"),
        spawnable=(),
        metadata={},
        default_model=None,
    )
    assert compiled == hand
    # Sanity: name doesn't match → specs shouldn't be equal
    wrong = dataclasses.replace(hand, name="renamed")
    assert compiled != wrong


# ---------------------------------------------------------------------------
# QueryResult materialises projections before shutdown
# ---------------------------------------------------------------------------


def _finish_only_options() -> Options:
    return Options(
        system_prompt="Return the requested text exactly.",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "resp-finish"},
    )


def test_query_large_answer_survives_shutdown(tmp_path: Path) -> None:
    """A spilled answer (answer_ref) is fully readable from the QueryResult
    after query() has torn the temporary Client + ContentStore down."""
    ws = _make_workspace(tmp_path)
    large_answer = "x" * 8000  # > _ANSWER_INLINE_LIMIT → spilled to the store
    provider = FakeLLMProvider(responses=[_end_turn(large_answer)])

    result = query(
        _finish_only_options(),
        goal="return the large answer",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    # Precondition: the terminal event really did spill the answer.
    completed = _envelopes_of_type(result, "TaskCompleted")
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    assert payload.answer is None
    assert payload.answer_ref is not None

    # The materialized accessors resolve the full answer byte-for-byte —
    # no Client, no ContentStore in sight.
    assert result.answer() == large_answer
    terminal_views = [v for v in result.messages() if isinstance(v, Result)]
    assert terminal_views == [Result(answer=large_answer, status="completed")]


def test_query_small_answer_inline_unchanged(tmp_path: Path) -> None:
    """A small answer rides inline on the envelope and takes the same
    accessors."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(responses=[_end_turn("done")])

    result = query(
        _finish_only_options(),
        goal="return done",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    completed = _envelopes_of_type(result, "TaskCompleted")
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    assert payload.answer == "done"
    assert payload.answer_ref is None
    assert result.answer() == "done"


def test_query_result_is_still_the_envelope_list(tmp_path: Path) -> None:
    """``QueryResult`` behaves as the plain envelope list callers iterate,
    index, and isinstance-check."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(responses=[_end_turn("done")])

    result = query(
        _finish_only_options(),
        goal="return done",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    assert isinstance(result, list)
    assert isinstance(result, QueryResult)
    assert len(result) > 0
    assert result[0].type == "TaskCreated"
    assert [e.type for e in result].count("TaskCompleted") == 1


def test_query_failed_task_answer_raises_coded_error(tmp_path: Path) -> None:
    """answer() is strict: a TaskFailed terminal raises QueryFailedError
    (code='query_failed') instead of handing back the failure reason as if it
    were an answer; the lenient view still exposes Result(status='failed')."""
    ws = _make_workspace(tmp_path)
    # A fatal error response → ReAct maps it to FailDecision → TaskFailed.
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="error",
                content=[],
                usage=Usage(uncached=1, output=0),
                raw={"category": "fatal"},
            )
        ]
    )

    result = query(
        _finish_only_options(),
        goal="this will fail",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )

    failed = _envelopes_of_type(result, "TaskFailed")
    assert len(failed) == 1
    assert isinstance(failed[0].payload, TaskFailedPayload)

    with pytest.raises(QueryFailedError) as excinfo:
        result.answer()
    err = excinfo.value
    assert err.code == "query_failed"
    assert err.status == "failed"
    assert err.reason == failed[0].payload.reason
    assert err.task_id == result.task_id

    terminal_views = [v for v in result.messages() if isinstance(v, Result)]
    assert terminal_views and terminal_views[0].status == "failed"
