"""A skill's ``allowed-tools`` grant is enforced on the tool call itself.

A workspace skill declaring ``allowed-tools: [Read]`` is activated and the
agent then proposes a ``write`` — outside the grant. ``skill_tool_enforcement``
decides the consequence: ``"approval"`` routes the call through the human
gate, ``"deny"`` refuses it outright. Without enforcement the grant would be
advisory text in a prompt, which a model is free to ignore.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


WRITE_CALL_ID = "w1"

_SKILL = """\
---
name: guarded
description: grants only Read
allowed-tools: [Read]
---
Only read tools are pre-approved.
"""


def _make_ws_with_skill(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".noeta" / "skills" / "guarded").mkdir(parents=True)
    (ws / ".noeta" / "skills" / "guarded" / "SKILL.md").write_text(
        _SKILL, encoding="utf-8"
    )
    return ws


def _responses() -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id=WRITE_CALL_ID,
                    tool_name="write",
                    arguments={"path": "new.py", "content": "x=1\n"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": WRITE_CALL_ID},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "end"},
        ),
    ]


def _session(ws: Path, *, mode: str):
    """A one-shot SDK host that enforces a skill's ``allowed-tools`` grant.

    ``require_approval_tools=()`` is load-bearing: it stops the host's default
    ``permission_mode`` from gating ``write`` as well, so the skill-grant
    enforcement is the only thing under test."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        skill_tool_enforcement=mode,  # type: ignore[arg-type]
        require_approval_tools=(),
    )
    return host, make_driver(host)


def test_approval_mode_gates_out_of_grant_write(tmp_path: Path) -> None:
    ws = _make_ws_with_skill(tmp_path)
    host, driver = _session(ws, mode="approval")
    out = driver.start(goal="write a file", agent="main", activations=("guarded",))
    # write is outside the skill's [Read] grant → gated, not executed.
    assert out.status == "suspended"
    assert not (ws / "new.py").exists()
    result = driver.approve(out.task_id, call_id=WRITE_CALL_ID)
    assert result.status == "terminal"
    assert (ws / "new.py").read_text() == "x=1\n"


def test_approval_mode_deny_refuses_write(tmp_path: Path) -> None:
    ws = _make_ws_with_skill(tmp_path)
    host, driver = _session(ws, mode="approval")
    out = driver.start(goal="write a file", agent="main", activations=("guarded",))
    result = driver.deny(
        out.task_id, call_id=WRITE_CALL_ID, reason="not granted"
    )
    assert result.status == "terminal"
    assert not (ws / "new.py").exists()


def test_deny_mode_denies_out_of_grant_write(tmp_path: Path) -> None:
    ws = _make_ws_with_skill(tmp_path)
    host, driver = _session(ws, mode="deny")
    out = driver.start(goal="write a file", agent="main", activations=("guarded",))
    # Hard deny: the call never runs and the loop still reaches terminal.
    assert out.status == "terminal"
    assert not (ws / "new.py").exists()
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "ToolCallDenied" in types
    assert "ToolResultRecorded" not in types
