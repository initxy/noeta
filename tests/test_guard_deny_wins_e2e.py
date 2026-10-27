"""A PreToolUse deny rule denies even when the call also needs approval.

In ``default`` mode a Bash command outside the allowlist needs approval
(``PermissionGuard``, priority 20) before the user ``HookGuard`` (priority 100)
is consulted. Approving the call must not execute what the deny rule forbids.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from noeta.protocols.messages import ToolUseBlock
from noeta.sdk import (
    Client,
    HooksConfig,
    HostConfig,
    LLMResponse,
    MatchArg,
    Options,
    PreToolUseRule,
    TextBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider


def _responses(cmd: str) -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="c1",
                    tool_name="Bash",
                    arguments={"command": cmd, "description": "x"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "2"},
        ),
    ]


@pytest.mark.parametrize("mode", ["default", "bypassPermissions"])
def test_deny_rule_beats_approved_call(tmp_path: Path, mode: str) -> None:
    marker = tmp_path / f"m_{mode}"
    rule = PreToolUseRule(
        "Bash",
        "deny",
        match_arg=MatchArg(
            path=("command",), op="regex", value="touch", pattern=re.compile("touch")
        ),
        reason="no touching",
    )
    approvals: list[str] = []

    def can_use_tool(name: str, args: object) -> bool:
        approvals.append(name)
        return True

    client = Client(
        Options(system_prompt="x", permission_mode=mode, can_use_tool=can_use_tool),
        provider=FakeLLMProvider(responses=_responses(f"touch {marker}")),
        workspace_dir=tmp_path,
        multi_turn=False,
        model="claude-sonnet-4-5",
        host_config=HostConfig(hooks=HooksConfig(pre_tool_use=(rule,))),
    )
    try:
        client.start(goal="go")
    finally:
        client.shutdown()
    assert not marker.exists()
    # A denied call is never put to the approver.
    assert approvals == []
