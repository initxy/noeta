"""``permission_mode``, ``can_use_tool``, and ``cwd`` wiring.

The permission mode decides which tool calls suspend for human approval, and
that decision is the only thing between a scripted model and an arbitrary write
or shell command — so both the pure ``_approval_set_for`` mapping and the
end-to-end suspend/approve path are pinned here. ``cwd`` and ``can_use_tool``
are wiring: they steer a run without entering agent identity.
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
    "Read",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "Bash",
]


def _builtin_refs(names=None):
    return [builtin_tool_ref(n) for n in (names or _ALL_TOOL_NAMES)]


def test_approval_set_default_gates_high_risk_only():
    refs = _builtin_refs()
    got = _approval_set_for("default", refs)
    # The high-risk built-ins, and only those.
    assert set(got) == {"Write", "Edit", "Bash"}


def test_approval_set_accept_edits_exempts_the_editors():
    refs = _builtin_refs()
    got = _approval_set_for("acceptEdits", refs)
    # Edit-class tools are exempted; shell_run stays gated.
    assert set(got) == {"Bash"}


def test_approval_set_bypass_empty():
    refs = _builtin_refs()
    assert _approval_set_for("bypassPermissions", refs) == ()


def test_approval_set_honours_custom_tool_risk_level():
    refs = _builtin_refs(["Read"]) + [
        ToolRef(name="delete_db", version="1", risk_level="high"),
        ToolRef(name="check_status", version="1", risk_level="low"),
    ]
    assert set(_approval_set_for("default", refs)) == {"delete_db"}
    # acceptEdits only carves out the three built-in editors; a custom
    # high-risk tool with any other name stays gated.
    assert set(_approval_set_for("acceptEdits", refs)) == {"delete_db"}


def test_approval_set_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unsupported permission_mode"):
        _approval_set_for("bogus", _builtin_refs(["Read"]))


# ---------------------------------------------------------------------------
# E2E: default mode suspends; manual approve/deny
# ---------------------------------------------------------------------------


def test_default_mode_write_suspends_then_approve_runs(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "Write", {"file_path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write",),
        permission_mode="default",
    )
    client = Client(options, provider=provider, workspace_dir=ws,
                    model="stub-model", multi_turn=False)
    try:
        outcome = client.start(goal="create new.txt")
        assert outcome.status == "suspended"
        assert outcome.wake_handle == "approval-w1"
        types = _types(client.events(outcome.task_id))
        assert "ToolCallApprovalRequested" in types
        assert "ToolResultRecorded" not in types

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
            _tooluse("w1", "Write", {"file_path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write",),
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
            _tooluse("w1", "Write", {"file_path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write", "Bash"),
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
    started = [e for e in envelopes if e.type == "ToolCallStarted"]
    assert any(e.payload.tool_name == "Write" for e in started
               if hasattr(e.payload, "tool_name"))


def test_accept_edits_pure_function_still_gates_shell_run():
    refs = _builtin_refs(["Write", "Edit", "Bash", "Read"])
    got = _approval_set_for("acceptEdits", refs)
    assert set(got) == {"Bash"}


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
        refs = [builtin_tool_ref(n) for n in ["Write", "Bash"]]
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
            _tooluse("w1", "Write", {"file_path": "new.txt", "content": "hi\n"}),
            _end("done"),
        ]
    )
    calls: list[tuple[str, dict]] = []

    def allow_all(tool_name: str, arguments: dict) -> bool:
        calls.append((tool_name, dict(arguments)))
        return True

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write",),
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
    assert calls == [("Write", {"file_path": "new.txt", "content": "hi\n"})]
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
    assert r.tool_name == "Write"
    assert "ToolResultRecorded" in types
    assert "TaskCompleted" in types


def test_can_use_tool_deny_records_and_tool_never_runs(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "Write", {"file_path": "new.txt", "content": "hi\n"}),
            _end("refusal handled"),
        ]
    )

    def deny_all(tool_name: str, arguments: dict) -> bool:
        return False

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write",),
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
    resolved = [
        e.payload for e in envelopes
        if e.type == "ToolCallApprovalResolved"
        and isinstance(e.payload, ToolCallApprovalResolvedPayload)
    ]
    assert len(resolved) == 1
    assert resolved[0].approved is False
    assert resolved[0].resolver == "can_use_tool"
    assert "ToolResultRecorded" not in types
    # A denial is feedback, not a failure: the model gets a second turn, which
    # is why the script carries a trailing end_turn response.
    assert "TaskCompleted" in types


def test_can_use_tool_drains_multiple_approvals_in_series(tmp_path: Path):
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[
            _tooluse("w1", "Write", {"file_path": "a.txt", "content": "a\n"}),
            _tooluse("w2", "Write", {"file_path": "b.txt", "content": "b\n"}),
            _end("done"),
        ]
    )
    calls: list[str] = []

    def allow(tool_name: str, arguments: dict) -> bool:
        calls.append(tool_name)
        return True

    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Write",),
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
    assert calls == ["Write", "Write"]
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
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
        cwd=str(ws),  # str is wrapped by Path()
    )
    client = Client(options, provider=provider, model="stub-model")
    try:
        assert client._host.workspace_dir == ws
    finally:
        client.shutdown()


def test_cwd_missing_everywhere_falls_back_to_the_process_cwd(tmp_path: Path):
    """No ``workspace_dir`` and no ``Options.cwd`` ⇒ the process working directory.

    ``SdkHost.workspace_dir`` defaults to ``Path.cwd()``, and the two layers
    have to agree: an agent that never touches the filesystem must not have to
    name a directory before it can answer anything.
    """
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
    )
    client = Client(options, provider=provider, model="stub-model")
    try:
        assert client._host.workspace_dir == Path.cwd()
    finally:
        client.shutdown()


def test_cwd_explicit_kwarg_takes_precedence(tmp_path: Path):
    ws_kwarg = tmp_path / "kwarg_ws"
    ws_kwarg.mkdir()
    ws_option = tmp_path / "option_ws"
    ws_option.mkdir()
    provider = FakeLLMProvider(responses=[_end("hi")])
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Read",),
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
        allowed_tools=("Read",),
        permission_mode="bypassPermissions",
        cwd=ws,
    )
    envelopes = query(options, goal="hi", provider=provider, model="stub-model")
    assert "TaskCompleted" in _types(envelopes)


# ---------------------------------------------------------------------------
# Identity invariance — can_use_tool / cwd do not affect identity
# ---------------------------------------------------------------------------


def test_identity_invariant_to_cwd_and_can_use_tool():
    def allow(tool_name: str, arguments: dict) -> bool:
        return True

    from noeta.client import compile_options

    base = Options(system_prompt=_PROMPT, allowed_tools=("Read",))
    with_wiring = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Read",),
        cwd="/some/path",
        can_use_tool=allow,
    )
    base_main, _ = compile_options(base)
    wired_main, _ = compile_options(with_wiring)
    assert base_main == wired_main


# ---------------------------------------------------------------------------
# Shell permission model: allowlist-or-approve under default, no gate under
# bypass. Approval is conditional per command, not per tool — the allowlist is
# external governance and never reaches the engine/event/replay path.
# ---------------------------------------------------------------------------


def test_default_mode_allowlisted_shell_runs_without_approval(tmp_path: Path):
    """A built-in-allowlisted command (``git status``) runs silently under
    ``default`` — no approval suspend, the tool executes."""
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[_tooluse("s1", "Bash", {"command": "git status"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Bash",),
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
    """A command outside the allowlist (``echo``) suspends for approval under
    ``default``; approving resumes and runs it."""
    ws = _ws(tmp_path)
    provider = FakeLLMProvider(
        responses=[_tooluse("s1", "Bash", {"command": "echo hi"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Bash",),
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
        responses=[_tooluse("s1", "Bash", {"command": "echo hi"}), _end()]
    )
    options = Options(
        system_prompt=_PROMPT,
        allowed_tools=("Bash",),
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
