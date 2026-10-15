"""Under ``bypassPermissions`` ``Write`` / ``Edit`` reach any absolute path.

Claude Code's bypass mode lets its file tools write outside the working
directory; refusing there sent models to ``Bash`` heredocs, which skip the
read-before-edit check. The fence still holds in the other modes, and the
read-first / staleness checks hold in every mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk import Client, HostConfig, Options
from noeta.testing.fake_llm import FakeLLMProvider


def _call(call_id: str, tool: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=tool, arguments=args)],
        usage=Usage(uncached=1, output=1),
    )


def _end() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
    )


def _run(tmp_path: Path, mode: str, responses: list[LLMResponse]) -> dict[str, Any]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    client = Client(
        Options(system_prompt="x", permission_mode=mode),
        provider=FakeLLMProvider(responses=responses),
        workspace_dir=ws,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(write_mode="apply"),
    )
    try:
        outcome = client.start(goal="go")
        folded = fold(
            client._host.event_log, client._host.content_store, outcome.task_id
        )
    finally:
        client.shutdown()
    return {
        b.call_id: b
        for m in folded.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    }


def test_bypass_writes_and_edits_outside_the_workspace(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "note.txt"
    results = _run(
        tmp_path,
        "bypassPermissions",
        [
            _call("w1", "Write", {"file_path": str(target), "content": "one\n"}),
            _call(
                "e0",
                "Edit",
                {"file_path": str(target), "old_string": "one", "new_string": "x"},
            ),
            _call("r1", "Read", {"file_path": str(target)}),
            _call(
                "e1",
                "Edit",
                {"file_path": str(target), "old_string": "one", "new_string": "two"},
            ),
            _end(),
        ],
    )
    assert results["w1"].success, results["w1"].error
    # A file it wrote is one it has seen, so the edit chains (Claude Code too).
    assert results["e0"].success, results["e0"].error
    assert results["e1"].success is False  # "one" is gone: the check ran
    assert target.read_text() == "x\n"


def test_bypass_still_refuses_an_unread_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep\n")
    results = _run(
        tmp_path,
        "bypassPermissions",
        [
            _call("w1", "Write", {"file_path": str(target), "content": "clobber"}),
            _end(),
        ],
    )
    assert results["w1"].success is False
    assert "has not been read yet" in (results["w1"].error or "")
    assert target.read_text() == "keep\n"


def test_accept_edits_keeps_the_workspace_fence(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.txt"
    results = _run(
        tmp_path,
        "acceptEdits",
        [
            _call("w1", "Write", {"file_path": str(target), "content": "x"}),
            _end(),
        ],
    )
    assert results["w1"].success is False
    assert "outside workspace" in (results["w1"].error or "")
    assert not target.exists()
