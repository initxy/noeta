"""Tests for as_messages view projection.

Covers three cases mandated by the issue:

1. happy path — query() produces the 4 canonical view types
   (UserMessage / ToolUse / ToolResultView / [AssistantMessage | Result]).
2. empty / noise-only streams — do not crash, return empty.
3. ToolUse dedup — same call_id does not double-appear when both
   MessagesAppended and ToolCallStarted carry it.
"""

from __future__ import annotations

from pathlib import Path


from noeta.client import (
    AssistantMessage,
    Options,
    Result,
    ToolResultView,
    ToolUse,
    UserMessage,
    as_messages,
    query,
)
from noeta.protocols.events import (
    EventEnvelope,
    TaskCreatedPayload,
    TaskFailedPayload,
)
from noeta.protocols.messages import (
    ImageBlock,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
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


# ---------------------------------------------------------------------------
# Case 1 — happy path: view types all present
# ---------------------------------------------------------------------------


def test_as_messages_happy_path_contains_four_view_types(
    tmp_path: Path,
) -> None:
    """A normal query stream yields all four canonical view classes."""
    ws = _make_workspace(tmp_path)
    provider = FakeLLMProvider(
        responses=_scripted_tooluse_then_finish(
            tool_name="edit",
            arguments={"path": "x.py", "old": "foo", "new": "bar"},
            answer="replacement done",
        )
    )
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("read", "edit"),
        permission_mode="bypassPermissions",
    )
    view = query(
        options,
        goal="change foo to bar in x.py",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    ).messages()

    # 1. Each target type appears at least once.
    type_set = {type(v).__name__ for v in view}
    # ToolUse: at least one (from ToolUseBlock, ToolCallStarted, or both merged).
    # ToolResultView: at least one (ToolResultRecorded or ToolResultBlock).
    # Result (TaskCompleted) or AssistantMessage: at least one.
    assert "ToolUse" in type_set, f"ToolUse missing, types seen: {type_set}"
    assert "ToolResultView" in type_set, (
        f"ToolResultView missing, types seen: {type_set}"
    )
    # At least one finish marker: AssistantMessage (end_turn assistant text)
    # or Result (TaskCompleted answer).
    assert (
        "AssistantMessage" in type_set or "Result" in type_set
    ), f"no finish marker, types seen: {type_set}"

    # 2. ToolUse points at edit.
    tool_uses = [v for v in view if isinstance(v, ToolUse)]
    assert tool_uses
    assert any(tu.tool_name == "edit" for tu in tool_uses)

    # 3. At least one ToolResultView with success=True.
    result_views = [v for v in view if isinstance(v, ToolResultView)]
    assert result_views
    assert any(rv.success for rv in result_views)

    # 4. Ordering: ToolUse comes before its ToolResultView.
    use_idx = next(
        i for i, v in enumerate(view)
        if isinstance(v, ToolUse) and v.tool_name == "edit"
    )
    res_idx = next(
        i for i, v in enumerate(view) if isinstance(v, ToolResultView)
    )
    assert use_idx < res_idx, (
        f"ToolUse (idx {use_idx}) must come before ToolResultView (idx {res_idx})"
    )


# ---------------------------------------------------------------------------
# Case 2 — empty / noise-only streams
# ---------------------------------------------------------------------------


def test_as_messages_empty_stream() -> None:
    """No envelopes → empty list; must not crash."""
    from noeta.storage.memory import InMemoryContentStore

    cs = InMemoryContentStore()
    assert as_messages([], cs) == []


def test_as_messages_only_task_created() -> None:
    """Only TaskCreated (a non-view event type) → empty list; no crash."""
    from noeta.storage.memory import InMemoryContentStore

    cs = InMemoryContentStore()
    env = EventEnvelope.build(
        task_id="t-noise",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="no-op",
            policy_name="stub",
        ),
        schema_version=1,
    )
    result = as_messages([env], cs)
    # TaskCreated produces no view item.
    assert result == []


def test_as_messages_task_failed() -> None:
    """TaskFailed → Result with status='failed'; no crash."""
    from noeta.storage.memory import InMemoryContentStore

    cs = InMemoryContentStore()
    env = EventEnvelope.build(
        task_id="t-fail",
        type="TaskFailed",
        payload=TaskFailedPayload(reason="boom", retryable=False),
    )
    view = as_messages([env], cs)
    assert len(view) == 1
    item = view[0]
    assert isinstance(item, Result)
    assert item.status == "failed"
    assert item.answer == "boom"


# ---------------------------------------------------------------------------
# Case 3 — ToolUse dedup
# ---------------------------------------------------------------------------


def test_tool_use_dedup_across_messagesappended_and_toolcallstarted(
    tmp_path: Path,
) -> None:
    """A ToolUseBlock + ToolCallStarted sharing a call_id appear once in the view.

    Normal FakeLLMProvider path: the assistant's ToolUseBlock is wrapped into
    MessagesAppended first, then Engine emits a ToolCallStarted event carrying
    the same call_id. Dedup keeps the first occurrence (usually the
    MessagesAppended path).
    """
    ws = _make_workspace(tmp_path)
    call_id = "dup-test"
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("edit",),
        permission_mode="bypassPermissions",
    )

    from noeta.client import Client

    client = Client(
        options,
        provider=FakeLLMProvider(
            responses=_scripted_tooluse_then_finish(
                tool_name="edit",
                arguments={"path": "x.py", "old": "foo", "new": "bar"},
                call_id=call_id,
            )
        ),
        workspace_dir=ws,
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="edit x.py")
        task_id = outcome.task_id
        stream_envelopes = list(client.events(task_id))
        # Both MessagesAppended and ToolCallStarted are present.
        type_names = {e.type for e in stream_envelopes}
        assert "MessagesAppended" in type_names
        assert "ToolCallStarted" in type_names
        # And both share the same call_id.
        tc_started = [
            e for e in stream_envelopes if e.type == "ToolCallStarted"
        ]
        assert any(
            e.payload.call_id == call_id for e in tc_started
        ), "expected call_id not found in ToolCallStarted"

        view = as_messages(stream_envelopes, client._host.content_store)
    finally:
        client.shutdown()

    # For call_id=="dup-test", ToolUse must appear exactly once in the view.
    uses_of_interest = [
        v for v in view if isinstance(v, ToolUse) and v.call_id == call_id
    ]
    assert (
        len(uses_of_interest) == 1
    ), (
        f"ToolUse call_id={call_id!r} appeared {len(uses_of_interest)} times; "
        f"should dedup to 1. view: {view}"
    )


# ---------------------------------------------------------------------------
# Extra: custom tool runs and Result shape
# ---------------------------------------------------------------------------


_GREET_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "additionalProperties": False,
}


@tool(
    name="greet_msgview",
    version="1",
    risk_level="low",
    input_schema=_GREET_SCHEMA,
)
def _greet(arguments: dict, ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    name = arguments.get("name", "friend")
    return ToolResult(success=True, output=f"hello {name}")


def test_result_completed_shape(tmp_path: Path) -> None:
    """TaskCompleted collapses into Result(status='completed', answer=...)."""
    ws = _make_workspace(tmp_path)
    options = Options(
        system_prompt=_PROMPT,
        name="g",
        allowed_tools=(_greet,),
    )
    from noeta.client import Client

    client = Client(
        options,
        provider=FakeLLMProvider(
            responses=_scripted_tooluse_then_finish(
                tool_name="greet_msgview",
                arguments={"name": "noeta"},
                call_id="g1",
                answer="FINAL_ANSWER",
            )
        ),
        workspace_dir=ws,
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="please greet noeta")
        task_id = outcome.task_id
        view = as_messages(
            list(client.events(task_id)), client._host.content_store
        )
    finally:
        client.shutdown()

    results = [v for v in view if isinstance(v, Result)]
    # TaskCompleted always lands at the end of the envelope stream.
    assert results, "no TaskCompleted/TaskFailed -> missing Result"
    final = results[-1]
    assert final.status == "completed"
    # TaskCompletedPayload.answer is run through str().
    assert "FINAL_ANSWER" in final.answer or final.answer == "FINAL_ANSWER"


# ---------------------------------------------------------------------------
# ImageBlock projection — additive, must not garble neighbors
# ---------------------------------------------------------------------------


def test_image_block_in_user_message_does_not_garble_adjacent_text() -> None:
    """``ImageBlock`` is
    not yet projected into the message view, but when sandwiched between text it
    must still flush the buffer, so the surrounding text is not wrongly merged
    into a single ``UserMessage``."""
    from noeta.client.messages import _project_one_message

    msg = Message(
        role="user",
        content=[
            TextBlock(text="before"),
            ImageBlock(
                source=ContentRef(
                    hash="e" * 64, size=10, media_type="image/png"
                )
            ),
            TextBlock(text="after"),
        ],
    )
    out: list = []
    _project_one_message(msg, out, set(), set())

    user_msgs = [v for v in out if isinstance(v, UserMessage)]
    # Each text span is its own item, never merged into "beforeafter".
    texts = [v.text for v in user_msgs]
    assert texts == ["before", "after"], f"text around image wrongly merged: {texts}"


def test_image_block_in_assistant_message_does_not_garble_adjacent_text() -> None:
    """Assistant path likewise: the image must flush the text buffer to avoid merging adjacent spans."""
    from noeta.client.messages import _project_one_message

    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="hello "),
            ImageBlock(
                source=ContentRef(
                    hash="f" * 64, size=10, media_type="image/jpeg"
                )
            ),
            TextBlock(text="world"),
        ],
    )
    out: list = []
    _project_one_message(msg, out, set(), set())

    assistant_msgs = [v for v in out if isinstance(v, AssistantMessage)]
    texts = [v.text for v in assistant_msgs]
    assert texts == ["hello ", "world"], f"text around image wrongly merged: {texts}"


# ---------------------------------------------------------------------------
# Case 4 — ToolResultView dedup (FIX B)
# ---------------------------------------------------------------------------


def test_tool_result_dedup_across_messagesappended_and_toolresultrecorded(
    tmp_path: Path,
) -> None:
    """A ToolResultBlock (MessagesAppended) + ToolResultRecorded sharing a
    call_id appear once in the view.

    Normal tool-call path:
    1. Engine packs the ToolResultBlock into a user/tool-role MessagesAppended
       fed back to the LLM;
    2. and emits a ToolResultRecorded event to persist the result.
    Both carry the same call_id; dedup keeps the first occurrence (usually the
    ToolResultBlock in MessagesAppended).
    """
    ws = _make_workspace(tmp_path)
    call_id = "dup-result-test"
    options = Options(
        system_prompt=_PROMPT,
        name="main",
        allowed_tools=("edit",),
        permission_mode="bypassPermissions",
    )

    from noeta.client import Client

    client = Client(
        options,
        provider=FakeLLMProvider(
            responses=_scripted_tooluse_then_finish(
                tool_name="edit",
                arguments={"path": "x.py", "old": "foo", "new": "bar"},
                call_id=call_id,
            )
        ),
        workspace_dir=ws,
        model="stub-model",
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="edit x.py")
        task_id = outcome.task_id
        stream_envelopes = list(client.events(task_id))
        # Both MessagesAppended and ToolResultRecorded are present.
        type_names = {e.type for e in stream_envelopes}
        assert "MessagesAppended" in type_names
        assert "ToolResultRecorded" in type_names
        # And both share the call_id (ToolResultRecorded includes the target).
        tr_recorded = [
            e for e in stream_envelopes if e.type == "ToolResultRecorded"
        ]
        assert any(
            e.payload.call_id == call_id for e in tr_recorded
        ), "expected call_id not found in ToolResultRecorded"

        view = as_messages(stream_envelopes, client._host.content_store)
    finally:
        client.shutdown()

    # For call_id=="dup-result-test", ToolResultView must appear once.
    results_of_interest = [
        v for v in view
        if isinstance(v, ToolResultView) and v.call_id == call_id
    ]
    assert len(results_of_interest) == 1, (
        f"ToolResultView call_id={call_id!r} appeared "
        f"{len(results_of_interest)} times; should dedup to 1. view: {view}"
    )

    # More generally: all ToolResultView call_ids in the view are distinct.
    all_result_call_ids = [
        v.call_id for v in view if isinstance(v, ToolResultView)
    ]
    assert len(all_result_call_ids) == len(set(all_result_call_ids)), (
        f"view has ToolResultView with duplicate call_id: {all_result_call_ids}"
    )
