"""Phase 4.5 Issue A — interactive tool-call approval through the SDK host.

Proves the runtime HITL approval contract end-to-end via the production
:class:`SdkHost` + :class:`InteractionDriver` (the same assembly the shipping
``noeta.agent`` backend drives via :class:`noeta.sdk.Client`):

* a real `write` call is gated (host ``require_approval_tools``) → the
  session suspends on `HumanResponseReceived(handle="approval-{call_id}")`
  and the file is **not** written yet;
* `driver.approve(...)` runs the recovered call — the file appears on disk
  and the loop finishes (terminal);
* `driver.deny(...)` records the resolution, appends a `role="tool"`
  denial-feedback message, never writes the file, and the loop still
  finishes.
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
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


WRITE_CALL_ID = "w1"


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _responses() -> list[LLMResponse]:
    return [
        _tool_call(
            WRITE_CALL_ID,
            "write",
            {"path": "new.py", "content": "print('hi')\n"},
        ),
        _end_turn("done"),
    ]


def _session(workspace: Path):
    """A one-shot (multi_turn=False) SDK host + driver that gates ``write``.

    ``require_approval_tools=("write",)`` is the host-level override (highest
    precedence after a per-turn permission_mode, which we never pass), so only
    ``write`` is gated — matching the old ``CodeSessionConfig`` knob.
    """
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=("write",),
    )
    return host, make_driver(host)


def test_write_suspends_for_approval_before_writing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _session(ws)
    out = driver.start(goal="create new.py", agent="main")
    # gated: suspended on the approval handle, file not yet on disk.
    assert out.status == "suspended"
    assert out.wake_handle == f"approval-{WRITE_CALL_ID}"
    assert not (ws / "new.py").exists()
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallApprovalRequested" in types
    assert "ToolResultRecorded" not in types


def test_approve_writes_file_and_finishes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _session(ws)
    out = driver.start(goal="create new.py", agent="main")
    result = driver.approve(
        out.task_id, call_id=WRITE_CALL_ID, resolver="host"
    )
    # the approved write actually ran; loop finished.
    assert (ws / "new.py").read_text() == "print('hi')\n"
    assert result.status == "terminal"
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallApprovalResolved" in types
    assert "ToolResultRecorded" in types


def test_deny_does_not_write_and_appends_feedback(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _session(ws)
    out = driver.start(goal="create new.py", agent="main")
    result = driver.deny(
        out.task_id,
        call_id=WRITE_CALL_ID,
        reason="no writes in prod",
        resolver="host",
    )
    # the file was never written; loop still finished.
    assert not (ws / "new.py").exists()
    assert result.status == "terminal"
    events = host.event_log.read(out.task_id)
    types = [e.type for e in events]
    assert "ToolCallApprovalResolved" in types
    assert "ToolCallDenied" not in types  # single-event deny
    # the write tool never ran.
    assert "ToolResultRecorded" not in types
    # a role="tool" denial-feedback message was appended for the call.
    folded = fold(host.event_log, host.content_store, out.task_id)
    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs, "expected a denial-feedback tool message"
    block = tool_msgs[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == WRITE_CALL_ID
    assert block.success is False
    assert block.error == "no writes in prod"
