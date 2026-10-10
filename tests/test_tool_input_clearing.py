"""Old tool calls lose their bulk too, and a cleared result says what it was.

A worker's ``Write`` / ``Edit`` calls carry whole file bodies in their
arguments, and the prune only ever cleared results, so the arguments rode every
request for the rest of the task. Under the same two valves that clear outputs,
a tool_use's large argument strings are now swapped for ``[cleared, N chars]``
— keys and small values kept — except in the newest assistant turn that called
a tool, which stays as sent. A cleared result's marker names the call it
answered, so the model knows it already read that file.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import ThreeSegmentComposer, _is_cleared_marker
from noeta.protocols.messages import Message, ToolResultBlock, ToolUseBlock
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.view import View
from noeta.storage.memory import InMemoryContentStore


class _Tool:
    risk_level = "low"
    input_schema: dict[str, Any] = {"type": "object", "additionalProperties": True}

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output="ok")


def _write_turn(call_id: str, path: str, body: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id=call_id,
                    tool_name="Write",
                    arguments={"file_path": path, "content": body},
                )
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=call_id, output="wrote it", success=True)],
        ),
    ]


def _read_turn(call_id: str, path: str, body: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    call_id=call_id, tool_name="Read", arguments={"file_path": path}
                )
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=call_id, output=body, success=True)],
        ),
    ]


def _compose(messages: list[Message], *, keep: int = 2) -> View:
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(messages)
    composer = ThreeSegmentComposer(
        system_prompt="sys",
        tools={"Read": _Tool("Read"), "Write": _Tool("Write")},
        content_store=InMemoryContentStore(),
        available_window=10,  # tiny: the microcompaction valve is open
        microcompact_keep_recent=keep,
    )
    return composer.compose(task)


def _uses(view: View) -> dict[str, dict[str, Any]]:
    return {
        b.call_id: b.arguments
        for m in view.segments[2].content
        for b in m.content
        if isinstance(b, ToolUseBlock)
    }


def _results(view: View) -> dict[str, Any]:
    return {
        b.call_id: b.output
        for m in view.segments[2].content
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }


def test_old_write_bodies_are_cleared_and_the_path_kept() -> None:
    msgs: list[Message] = []
    for i in range(4):
        msgs += _write_turn(f"w{i}", f"/src/f{i}.py", "x" * 5_000)
    uses = _uses(_compose(msgs, keep=2))
    for i in range(2):
        assert uses[f"w{i}"] == {
            "file_path": f"/src/f{i}.py",
            "content": "[cleared, 5000 chars]",
        }
    for i in range(2, 4):
        assert uses[f"w{i}"]["content"] == "x" * 5_000


def test_small_arguments_are_left_alone() -> None:
    msgs: list[Message] = []
    for i in range(4):
        msgs += _write_turn(f"w{i}", f"/src/f{i}.py", "short body")
    view = _compose(msgs, keep=1)
    assert all(a["content"] == "short body" for a in _uses(view).values())


def test_nested_strings_are_cleared_in_place() -> None:
    big = {"edits": [{"old_string": "a" * 900, "new_string": "b"}], "file_path": "/x"}
    msgs = [
        Message(
            role="assistant",
            content=[ToolUseBlock(call_id="e0", tool_name="MultiEdit", arguments=big)],
        ),
        Message(role="tool", content=[ToolResultBlock(call_id="e0", output="ok", success=True)]),
        *_write_turn("w1", "/y", "z" * 10),
        *_write_turn("w2", "/z", "z" * 10),
    ]
    uses = _uses(_compose(msgs, keep=1))
    assert uses["e0"] == {
        "edits": [{"old_string": "[cleared, 900 chars]", "new_string": "b"}],
        "file_path": "/x",
    }


def test_the_newest_tool_calling_turn_is_never_cleared() -> None:
    """Even when the newest turn holds more calls than the valve keeps, it
    stays as sent: it is the turn whose reasoning a provider checks."""
    calls = [
        ToolUseBlock(
            call_id=f"p{j}",
            tool_name="Write",
            arguments={"file_path": f"/p{j}", "content": "q" * 2_000},
        )
        for j in range(3)
    ]
    msgs = [
        *_write_turn("w0", "/old", "o" * 2_000),
        Message(role="assistant", content=list(calls)),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=f"p{j}", output="ok", success=True) for j in range(3)],
        ),
    ]
    uses = _uses(_compose(msgs, keep=1))
    assert uses["w0"]["content"] == "[cleared, 2000 chars]"
    for j in range(3):
        assert uses[f"p{j}"]["content"] == "q" * 2_000


def test_a_cleared_result_names_the_file_it_read() -> None:
    msgs: list[Message] = []
    for i in range(3):
        msgs += _read_turn(f"r{i}", f"/src/mod{i}.py", "line\n" * 200)
    out = _results(_compose(msgs, keep=1))
    assert out["r0"] == "[tool output cleared: Read(/src/mod0.py), 1000 chars]"
    assert _is_cleared_marker(out["r0"])
    assert out["r2"] == "line\n" * 200


def test_recompose_is_idempotent_and_deterministic() -> None:
    msgs: list[Message] = []
    for i in range(3):
        msgs += _write_turn(f"w{i}", f"/f{i}", "x" * 1_000)
        msgs += _read_turn(f"r{i}", f"/f{i}", "y" * 1_000)
    first = _compose(msgs, keep=1)
    second = _compose(msgs, keep=1)
    assert first.segments[2].segment_hash == second.segments[2].segment_hash
    # Re-composing an already-cleared history changes nothing more.
    again = _compose(list(first.segments[2].content), keep=1)
    assert _uses(again) == _uses(first)
    assert _results(again) == _results(first)


def test_nothing_is_cleared_while_the_valves_are_shut() -> None:
    msgs: list[Message] = []
    for i in range(4):
        msgs += _write_turn(f"w{i}", f"/f{i}", "x" * 5_000)
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(msgs)
    view = ThreeSegmentComposer(
        system_prompt="sys",
        tools={"Write": _Tool("Write")},
        content_store=InMemoryContentStore(),
        available_window=10_000_000,
        microcompact_keep_recent=1,
    ).compose(task)
    assert all(a["content"] == "x" * 5_000 for a in _uses(view).values())


def test_the_relief_valve_alone_clears_arguments_outside_its_tail() -> None:
    msgs: list[Message] = []
    for i in range(4):
        msgs += _write_turn(f"w{i}", f"/f{i}", "x" * 5_000)
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(msgs)
    view = ThreeSegmentComposer(
        system_prompt="sys",
        tools={"Write": _Tool("Write")},
        content_store=InMemoryContentStore(),
        tail_token_budget=10,
        available_window=10,
        microcompact_keep_recent=None,
    ).compose(task)
    uses = _uses(view)
    assert uses["w0"]["content"] == "[cleared, 5000 chars]"
    assert uses["w3"]["content"] == "x" * 5_000
