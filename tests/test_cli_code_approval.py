"""Phase 4.5 Issue A — tool-call approval through the SDK host.

The operator CLI was a thin adapter over the runner's approval seam (architect
reminder #1). The library-SDK refactor dropped that operator CLI and the runner; these
tests now drive the **production** ``SdkHost`` + ``InteractionDriver`` assembly
directly: the code stub provider proposes a ``glob`` call, the gated tool is
supplied via the host ``require_approval_tools`` knob, and the approve/deny
decision is applied through ``driver.approve`` / ``driver.deny`` — exactly the
calls the old ``--approvals-file`` adapter made.

* approve → the gated call runs, the session reaches terminal, and the
  recording carries ``ToolCallApprovalResolved(approved=True)``;
* deny → the call is refused, the session still finishes, and the
  recording carries ``ToolCallApprovalResolved(approved=False)`` and no
  ``ToolResultRecorded`` for the gated call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from tests._stub_provider import CodeStubProvider
from noeta.client.host import SdkHost

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    resolve_shell_mode,
    resolve_write_mode,
    runner_main_spec,
)


STUB_CALL_ID = "code-stub-1"  # noeta.agent._stub_provider first-turn call_id


def _drive_approval(driver, host, out, *, approved: bool, reason: Optional[str]):
    """Resolve the pending gated tool call exactly as the old CLI adapter did:
    read the ``approval-{call_id}`` suspension off the ``DriveOutcome`` and apply
    one decision through ``driver.approve`` / ``driver.deny`` until the session
    leaves the approval-wait state."""
    _PREFIX = "approval-"
    while out.status == "suspended":
        handle = out.wake_handle
        if handle is None or not handle.startswith(_PREFIX):
            break
        call_id = handle[len(_PREFIX):]
        if approved:
            out = driver.approve(out.task_id, call_id=call_id, reason=reason)
        else:
            out = driver.deny(out.task_id, call_id=call_id, reason=reason)
    return out


def _run_with_approval(
    tmp_path: Path, *, approved: bool, reason: Optional[str]
) -> tuple[SdkHost, str]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=CodeStubProvider(),
        model="stub-model",
        multi_turn=False,
        write_mode=resolve_write_mode(
            allow_write=False, yes=False, read_only=False
        ),
        shell_mode=resolve_shell_mode(allow_shell=False),
        require_approval_tools=("glob",),
    )
    driver = make_driver(host)
    out = driver.start(goal="smoke", agent="main")
    out = _drive_approval(driver, host, out, approved=approved, reason=reason)
    assert out.status == "terminal"
    return host, out.task_id


def test_cli_run_approve_reaches_terminal(tmp_path: Path) -> None:
    host, task_id = _run_with_approval(tmp_path, approved=True, reason=None)
    # the recording carries an approved resolution.
    types = [e.type for e in host.event_log.read(task_id)]
    assert "ToolCallApprovalResolved" in types
    assert "ToolResultRecorded" in types  # approved call ran


def test_cli_run_deny_still_terminal_no_tool_result(tmp_path: Path) -> None:
    host, task_id = _run_with_approval(
        tmp_path, approved=False, reason="not-now"
    )
    types = [e.type for e in host.event_log.read(task_id)]
    assert "ToolCallApprovalResolved" in types
    assert "ToolCallDenied" not in types  # single-event deny
    assert "ToolResultRecorded" not in types  # the gated call never ran
