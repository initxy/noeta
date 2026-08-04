"""Interactive tool-call approval through the SDK host.

The human-in-the-loop contract end-to-end via :class:`SdkHost` +
:class:`InteractionDriver`: a gated ``write`` suspends the task on
``approval-{call_id}`` with nothing yet on disk, ``approve`` runs the
recovered call, and ``deny`` records the resolution and appends a
``role="tool"`` denial-feedback message so the model learns why it was
refused. Both resolutions must still reach terminal — a gate that could
strand a task would be worse than no gate.
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
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

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
            "Write",
            {"file_path": "new.py", "content": "print('hi')\n"},
        ),
        _end_turn("done"),
    ]


def _session(workspace: Path):
    """A one-shot (multi_turn=False) SDK host + driver that gates ``write``.

    ``require_approval_tools=("Write",)`` is the host-level override — highest
    precedence after a per-turn ``permission_mode``, which these tests never
    pass — so ``write`` and nothing else is gated.
    """
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=("Write",),
    )
    return host, make_driver(host)


def test_write_suspends_for_approval_before_writing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _session(ws)
    out = driver.start(goal="create new.py", agent="main")
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
    assert not (ws / "new.py").exists()
    assert result.status == "terminal"
    events = host.event_log.read(out.task_id)
    types = [e.type for e in events]
    assert "ToolCallApprovalResolved" in types
    # A deny is one resolution event, not a resolution plus a denial event.
    assert "ToolCallDenied" not in types
    assert "ToolResultRecorded" not in types
    folded = fold(host.event_log, host.content_store, out.task_id)
    tool_msgs = [m for m in folded.runtime.messages if m.role == "tool"]
    assert tool_msgs, "expected a denial-feedback tool message"
    block = tool_msgs[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.call_id == WRITE_CALL_ID
    assert block.success is False
    assert block.error == "no writes in prod"
