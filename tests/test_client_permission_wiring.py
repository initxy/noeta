"""Tests for the library-SDK wiring: Options.permission_mode,
Options.can_use_tool, Options.cwd.

Covers every combination the spec requires (three modes, plan removed):

* ``_approval_set_for`` pure-function mapping (three permission modes,
  mix of built-in + custom tool refs).
* End-to-end via ``Client`` / ``query`` + ``FakeLLMProvider`` scripted
  responses:
  - ``default`` — high-risk tool suspends with
    ``ToolCallApprovalRequested``; manual ``approve`` resumes and runs
    the tool.
  - ``bypassPermissions`` — same tool script runs to terminal with no
    approval event.
  - ``acceptEdits`` — ``write`` runs without approval, the pure
    function still gates ``shell_run``.
  - ``can_use_tool`` callback auto-approves → task completes with a
    ``ToolCallApprovalResolved(approved=True, resolver="can_use_tool")``;
    callback auto-denies → same event shape with ``approved=False`` and
    the tool never ran.
* ``Client`` + ``query`` ``workspace_dir`` resolution: explicit kwarg
  wins, ``Options.cwd`` fallback works, neither set → ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.agent.spec import ToolRef
from noeta.client import (
    Client,
    Options,
    query,
)
from noeta.client.host import _approval_set_for
from noeta.client.parts import builtin_tool_ref
from noeta.protocols.events import (
    ToolCallApprovalResolvedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PROMPT = "You are a test agent. Do what the user asks."


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")
    return ws


def _tooluse(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _types(events):
    return [e.type for e in events]


# ---------------------------------------------------------------------------
# _approval_set_for pure-function tests
# ---------------------------------------------------------------------------


_ALL_TOOL_NAMES = [
    "read",
    "glob",
    "grep",
    "write",
    "edit",
    "apply_patch",
    "shell_run",
]


def _builtin_refs(names=None):
    return [builtin_tool_ref(n) for n in (names or _ALL_TOOL_NAMES)]


def test_approval_set_default_gates_high_risk_only():
    refs = _builtin_refs()
    got = _approval_set_for("default", refs)
    # All and only the four "high risk" built-ins.
    assert set(got) == {"write", "edit", "apply_patch", "shell_run"}


def test_approval_set_accept_edits_exempts_three_editors():
    refs = _builtin_refs()
    got = _approval_set_for("acceptEdits", refs)
    # Edit-class tools are exempted; shell_run is still high-risk and gated.
    assert set(got) == {"shell_run"}


def test_approval_set_bypass_empty():
    refs = _builtin_refs()
    assert _approval_set_for("bypassPermissions", refs) == ()


def test_approval_set_honours_custom_tool_risk_level():
    # A custom tool declared high-risk should be gated in default mode.
    refs = _builtin_refs(["read"]) + [
        ToolRef(name="delete_db", version="1", risk_level="high"),
        ToolRef(name="check_status", version="1", risk_level="low"),
    ]
    assert set(_approval_set_for("default", refs)) == {"delete_db"}
    # acceptEdits only carves out the three built-in editors; a custom
    # high-risk tool with any other name stays gated.
    assert set(_approval_set_for("acceptEdits", refs)) == {"delete_db"}


def test_approval_set_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unsupported permission_mode"):
        _approval_set_for("bogus", _builtin_refs(["read"]))


# ---------------------------------------------------------------------------
# E2E: default mode suspends; manual approve/deny
# ---------------------------------------------------------------------------


def test_default_mode_write_suspends_then_approve_runs(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write",),
        permission_mode="default",
    )
    client = Client(options, provider=provider, workspace_dir=ws,
                    model="stub-model", multi_turn=False)
    try:
        outcome = client.start(goal="create new.txt")
        # Suspended on the approval handle.
        assert outcome.status == "suspended"
        assert outcome.wake_handle == "approval-w1"
        types = _types(client.events(outcome.task_id))
        assert "ToolCallApprovalRequested" in types
        assert "ToolResultRecorded" not in types

        # Approve → task completes, tool ran.
        outcome2 = client.approve(outcome.task_id, call_id="w1")
        assert outcome2.status == "terminal"
        types2 = _types(client.events(outcome.task_id))
        assert "ToolCallApprovalResolved" in types2
        assert "ToolResultRecorded" in types2
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# E2E: bypassPermissions → direct finish, no approval event
# ---------------------------------------------------------------------------


def test_bypass_mode_write_runs_without_approval(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write",),
        permission_mode="bypassPermissions",
    )
    envelopes = query(
        options,
        goal="create new.txt",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    types = _types(envelopes)
    assert "ToolCallApprovalRequested" not in types
    assert "ToolCallApprovalResolved" not in types
    assert "ToolResultRecorded" in types
    assert "TaskCompleted" in types


# ---------------------------------------------------------------------------
# E2E: acceptEdits — write goes through; pure-function asserts shell_run
# ---------------------------------------------------------------------------


def test_accept_edits_write_runs_without_approval(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write", "shell_run"),
        permission_mode="acceptEdits",
    )
    envelopes = query(
        options,
        goal="create new.txt",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    types = _types(envelopes)
    assert "ToolCallApprovalRequested" not in types
    assert "TaskCompleted" in types
    # write actually ran.
    started = [e for e in envelopes if e.type == "ToolCallStarted"]
    assert any(e.payload.tool_name == "write" for e in started
               if hasattr(e.payload, "tool_name"))


def test_accept_edits_pure_function_still_gates_shell_run():
    refs = _builtin_refs(["write", "edit", "apply_patch", "shell_run",
                          "read"])
    got = _approval_set_for("acceptEdits", refs)
    assert set(got) == {"shell_run"}


def test_bypass_mode_pure_function_stores_empty_gate(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        permission_mode="bypassPermissions",
    )
    client = Client(
        options, provider=provider, workspace_dir=ws,
        model="stub-model", multi_turn=False,
    )
    try:
        assert client._host.permission_mode == "bypassPermissions"
        refs = [builtin_tool_ref(n) for n in ["write", "shell_run"]]
        assert _approval_set_for("bypassPermissions", refs) == ()
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# can_use_tool auto-resolver
# ---------------------------------------------------------------------------


def test_can_use_tool_allow_completes_and_records_resolver(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    calls: list[tuple[str, dict]] = []

    def allow_all(tool_name: str, arguments: dict) -> bool:
        calls.append((tool_name, dict(arguments)))
        return True

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write",),
        permission_mode="default",
        can_use_tool=allow_all,
    )
    envelopes = query(
        options,
        goal="create new.txt",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    types = _types(envelopes)
    # Callback saw the call.
    assert calls == [("write", {"path": "new.txt", "content": "hi\n"})]
    # Resolver recorded with correct metadata.
    resolved = [
        e.payload for e in envelopes
        if e.type == "ToolCallApprovalResolved"
        and isinstance(e.payload, ToolCallApprovalResolvedPayload)
    ]
    assert len(resolved) == 1
    r = resolved[0]
    assert r.approved is True
    assert r.resolver == "can_use_tool"
    assert r.call_id == "w1"
    assert r.tool_name == "write"
    # The tool actually ran and the task finished.
    assert "ToolResultRecorded" in types
    assert "TaskCompleted" in types


def test_can_use_tool_deny_records_and_tool_never_runs(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "new.txt", "content": "hi\n"}),
            _end("refusal handled"),
        ]
    )

    def deny_all(tool_name: str, arguments: dict) -> bool:
        return False

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write",),
        permission_mode="default",
        can_use_tool=deny_all,
    )
    envelopes = query(
        options,
        goal="create new.txt",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    types = _types(envelopes)
    # Deny resolution was recorded.
    resolved = [
        e.payload for e in envelopes
        if e.type == "ToolCallApprovalResolved"
        and isinstance(e.payload, ToolCallApprovalResolvedPayload)
    ]
    assert len(resolved) == 1
    assert resolved[0].approved is False
    assert resolved[0].resolver == "can_use_tool"
    # Tool never ran.
    assert "ToolResultRecorded" not in types
    # But the loop still finished (model emitted a second response after
    # receiving the deny feedback).
    assert "TaskCompleted" in types


def test_can_use_tool_drains_multiple_approvals_in_series(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "write", {"path": "a.txt", "content": "a\n"}),
            _tooluse("w2", "write", {"path": "b.txt", "content": "b\n"}),
            _end("done"),
        ]
    )
    calls: list[str] = []

    def allow(tool_name: str, arguments: dict) -> bool:
        calls.append(tool_name)
        return True

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("write",),
        permission_mode="default",
        can_use_tool=allow,
    )
    envelopes = query(
        options,
        goal="create both files",
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
    )
    # Both pending approvals were auto-resolved, no suspend leaks out.
    assert calls == ["write", "write"]
    resolved = [
        e for e in envelopes if e.type == "ToolCallApprovalResolved"
    ]
    assert len(resolved) == 2
    assert "TaskCompleted" in _types(envelopes)


# ---------------------------------------------------------------------------
# cwd wiring — Options.cwd and precedence
# ---------------------------------------------------------------------------


def test_cwd_uses_options_cwd_when_kwarg_missing(tmp_path: Path):
    ws = tmp_path / "opts_ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
        cwd=str(ws),  # str is wrapped by Path()
    )
    # No workspace_dir= kwarg → should use Options.cwd.
    client = Client(options, provider=provider, model="stub-model")
    try:
        assert client._host.workspace_dir == ws
    finally:
        client.shutdown()


def test_cwd_missing_everywhere_raises(tmp_path: Path):
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )
    with pytest.raises(ValueError, match="workspace directory is required"):
        Client(options, provider=provider, model="stub-model")


def test_cwd_explicit_kwarg_takes_precedence(tmp_path: Path):
    ws_kwarg = tmp_path / "kwarg_ws"
    ws_kwarg.mkdir()
    ws_option = tmp_path / "option_ws"
    ws_option.mkdir()
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
        cwd=ws_option,
    )
    client = Client(
        options,
        provider=provider,
        workspace_dir=ws_kwarg,
        model="stub-model",
    )
    try:
        assert client._host.workspace_dir == ws_kwarg
    finally:
        client.shutdown()


def test_query_uses_options_cwd(tmp_path: Path):
    ws = tmp_path / "query_ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
        cwd=ws,
    )
    # No workspace_dir kwarg → should not raise and complete cleanly.
    envelopes = query(options, goal="hi", provider=provider, model="stub-model")
    assert "TaskCompleted" in _types(envelopes)


# ---------------------------------------------------------------------------
# Identity invariance — can_use_tool / cwd do not affect identity
# ---------------------------------------------------------------------------


def test_identity_invariant_to_cwd_and_can_use_tool():
    def allow(tool_name: str, arguments: dict) -> bool:
        return True

    from noeta.client import compile_options

    base = Options(system_prompt=_PROMPT, allowed_tools=("read",))
    with_wiring = Options(
        system_prompt=_PROMPT,
        allowed_tools=("read",),
        cwd="/some/path",
        can_use_tool=allow,
    )
    base_main, _ = compile_options(base)
    wired_main, _ = compile_options(with_wiring)
    assert base_main == wired_main


# ---------------------------------------------------------------------------
# Shell permission model: allowlist-or-approve under default; bypass = no gate
# (per-command conditional approval; the allowlist is external governance, the
# engine/event/replay path is untouched).
# ---------------------------------------------------------------------------


def test_default_mode_allowlisted_shell_runs_without_approval(tmp_path: Path):
    """A built-in-allowlisted command (``git status``) runs silently under
    ``default`` — no approval suspend, the tool executes."""
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[_tooluse("s1", "shell_run", {"command": "git status"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("shell_run",),
        permission_mode="default",
    )
    envelopes = query(
        options, goal="check status", provider=provider,
        workspace_dir=ws, model="stub-model",
    )
    types = _types(envelopes)
    assert "ToolCallApprovalRequested" not in types
    assert "ToolResultRecorded" in types


def test_default_mode_unlisted_shell_suspends_then_approve_runs(tmp_path: Path):
    """A command NOT in the allowlist (``echo``) suspends for approval under
    ``default``; approving resumes and runs it (reuses the Issue A HITL path —
    no new event types)."""
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[_tooluse("s1", "shell_run", {"command": "echo hi"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("shell_run",),
        permission_mode="default",
    )
    client = Client(options, provider=provider, workspace_dir=ws,
                    model="stub-model", multi_turn=False)
    try:
        outcome = client.start(goal="say hi")
        assert outcome.status == "suspended"
        assert outcome.wake_handle == "approval-s1"
        assert "ToolCallApprovalRequested" in _types(client.events(outcome.task_id))

        outcome2 = client.approve(outcome.task_id, call_id="s1")
        assert outcome2.status == "terminal"
        types2 = _types(client.events(outcome.task_id))
        assert "ToolCallApprovalResolved" in types2
        assert "ToolResultRecorded" in types2
    finally:
        client.shutdown()


def test_bypass_mode_unlisted_shell_runs_without_approval(tmp_path: Path):
    """Under ``bypassPermissions`` even a non-allowlisted command runs with no
    approval gate at all (ARBITRARY)."""
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[_tooluse("s1", "shell_run", {"command": "echo hi"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("shell_run",),
        permission_mode="bypassPermissions",
    )
    envelopes = query(
        options, goal="say hi", provider=provider,
        workspace_dir=ws, model="stub-model",
    )
    types = _types(envelopes)
    assert "ToolCallApprovalRequested" not in types
    assert "ToolResultRecorded" in types
    assert "TaskCompleted" in types
