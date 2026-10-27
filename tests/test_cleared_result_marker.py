"""A cleared result reads Claude Code's marker, and a tool call keeps its
arguments.

The prune clears old tool outputs only — a ``Write``'s file body stays in the
call that wrote it until the summary covers it, as in Claude Code. The call a
cleared result answered still names the file, so the model knows it already
read it.
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
    clearable = True
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
        microcompact_batch_tokens=1,  # every aged-out result is its own batch
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


def test_old_tool_calls_keep_their_arguments_when_the_valve_is_open() -> None:
    msgs: list[Message] = []
    for i in range(4):
        msgs += _write_turn(f"w{i}", f"/src/f{i}.py", "x" * 5_000)
    uses = _uses(_compose(msgs, keep=1))
    assert all(uses[f"w{i}"]["content"] == "x" * 5_000 for i in range(4))


def test_a_cleared_result_reads_claude_codes_marker() -> None:
    msgs: list[Message] = []
    for i in range(3):
        msgs += _read_turn(f"r{i}", f"/src/mod{i}.py", "line\n" * 200)
    view = _compose(msgs, keep=1)
    out = _results(view)
    assert out["r0"] == "[Old tool result content cleared]"
    # The call it answered still names the file.
    assert _uses(view)["r0"] == {"file_path": "/src/mod0.py"}
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
