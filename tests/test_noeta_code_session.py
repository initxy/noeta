"""A one-shot coding session, and the closures that keep it safe by default.

Drives the production assembly (``SdkHost`` + ``InteractionDriver``) with a
scripted provider. Every closure here is enforced at construction rather than
by a runtime refusal: dry-run leaves the workspace untouched while still
recording the proposed diff, an agent's tool list filters the pack before the
Engine sees it, and ``ShellMode.OFF`` removes shell from the pack outright.
``resolve_write_mode`` / ``resolve_shell_mode`` pin the flag precedence that
picks those modes, since a wrong answer there is what would let a read-only
request write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._skill_fixtures import write_skill_raw

from noeta.presets import official_specs
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    resolve_shell_mode,
    resolve_write_mode,
    runner_main_spec,
    session_result,
)


_SKILL_FIX_PYTHON_TEST = """\
---
name: fix-python-test
description: minimal-patch loop for a failing pytest
priority: 50
---
1. Run pytest to surface the failing test.
2. Read the failing file, locate the bug.
3. Apply the smallest possible edit patch.
4. Rerun pytest to confirm.
"""


def _replace_then_finish(*, path: str = "x.py", old: str = "foo", new: str = "bar") -> list[LLMResponse]:
    return [
        # The Read satisfies Edit's read-first precondition, as a real
        # session's flow would.
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="rt-0",
                    tool_name="Read",
                    arguments={"file_path": path},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-0"},
        ),
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="rt-1",
                    tool_name="Edit",
                    arguments={"file_path": path, "old_string": old, "new_string": new},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-2"},
        ),
    ]


def _make_workspace(tmp_path: Path, *, with_skill: bool = False) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "x.py").write_text("foo\n")
    if with_skill:
        write_skill_raw(workspace / ".noeta" / "skills", "fix-python-test", _SKILL_FIX_PYTHON_TEST)
    return workspace


# ---------------------------------------------------------------------------
# resolve_write_mode / resolve_shell_mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "allow_write,yes,read_only,expected",
    [
        (False, False, False, FsWriteMode.DRY_RUN),  # default safe
        (True, False, False, FsWriteMode.DRY_RUN),   # missing --yes
        (False, True, False, FsWriteMode.DRY_RUN),   # missing --allow-write
        (True, True, False, FsWriteMode.APPLY),      # both flags → apply
        (True, True, True, FsWriteMode.DRY_RUN),     # --read-only overrides
    ],
)
def test_resolve_write_mode_precedence(
    allow_write: bool, yes: bool, read_only: bool, expected: FsWriteMode
) -> None:
    assert (
        resolve_write_mode(allow_write=allow_write, yes=yes, read_only=read_only)
        is expected
    )


@pytest.mark.parametrize(
    "allow_shell,shell_off,expected",
    [
        (False, False, ShellMode.ALLOWLIST),
        (True, False, ShellMode.ARBITRARY),
        (False, True, ShellMode.OFF),
        (True, True, ShellMode.OFF),  # off wins
    ],
)
def test_resolve_shell_mode_precedence(
    allow_shell: bool, shell_off: bool, expected: ShellMode
) -> None:
    assert (
        resolve_shell_mode(allow_shell=allow_shell, shell_off=shell_off)
        is expected
    )


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


def test_default_agent_exposes_full_fs_pack() -> None:
    """``main`` allows every built-in tool.

    ``web_search`` is always in the whitelist; the runtime pack only
    materialises it when a search API key is configured.
    """
    expected = {
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "Bash",
        "BashOutput",
        "KillShell",
        "WebFetch",
        "WebSearch",
    }
    assert frozenset(r.name for r in official_specs()["main"].tools) == expected


def test_get_agent_unknown_raises() -> None:
    with pytest.raises(KeyError):
        official_specs()["does-not-exist"]
    assert "main" in official_specs()


# ---------------------------------------------------------------------------
# Session driver — one-shot helper
# ---------------------------------------------------------------------------


def _session(
    workspace: Path,
    *,
    responses: list[LLMResponse],
    agent: str = "main",
    write_mode: FsWriteMode = FsWriteMode.APPLY,
    shell_mode: ShellMode = ShellMode.OFF,
    sqlite_path: str | None = None,
):
    """A one-shot SDK host + driver. ``require_approval_tools=()`` so the host's
    default permission gate does not pause the edit family for approval (the old
    one-shot runner applied edits without a gate)."""
    host = make_host(
        make_registry(runner_main_spec(agent)),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=write_mode,
        shell_mode=shell_mode,
        require_approval_tools=(),
        sqlite_path=sqlite_path,
    )
    return host, make_driver(host)


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------


def test_runner_apply_edits_workspace(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    target = workspace / "x.py"
    host, driver = _session(
        workspace, responses=_replace_then_finish(),
        write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
    )
    out = driver.start(goal="rename foo to bar", agent="main")
    result = session_result(host, out)
    assert result.status == "terminal"
    assert target.read_text() == "bar\n"
    assert len(result.files_changed) == 1
    change = result.files_changed[0]
    assert change["tool"] == "Edit"
    assert change["path"] == "x.py"
    assert change["applied"] is True
    assert result.last_shell is None  # no shell calls scripted


def test_runner_dry_run_does_not_edit_workspace(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    target = workspace / "x.py"
    original = target.read_bytes()
    host, driver = _session(
        workspace, responses=_replace_then_finish(),
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.OFF,
    )
    out = driver.start(goal="rename foo to bar", agent="main")
    result = session_result(host, out)
    assert result.status == "terminal"
    # File on disk untouched.
    assert target.read_bytes() == original
    # But the proposed-diff was recorded.
    assert len(result.files_changed) == 1
    assert result.files_changed[0]["applied"] is False


# ---------------------------------------------------------------------------
# Skill activation surfaces in the summary
# ---------------------------------------------------------------------------


def test_runner_records_selected_skills_from_workspace_pack(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, with_skill=True)
    host, driver = _session(
        workspace, responses=_replace_then_finish(),
        write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
    )
    out = driver.start(
        goal="rename foo to bar", agent="main", activations=("fix-python-test",)
    )
    result = session_result(host, out)
    assert result.status == "terminal"
    assert result.selected_skills == ("fix-python-test",)


def test_runner_empty_activation_is_noop(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    host, driver = _session(
        workspace, responses=_replace_then_finish(),
        write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
    )
    out = driver.start(goal="rename foo to bar", agent="main")
    result = session_result(host, out)
    assert result.selected_skills == ()


# ---------------------------------------------------------------------------
# Shell tool gating
# ---------------------------------------------------------------------------


def test_runner_shell_off_removes_shell_run_from_pack(tmp_path: Path) -> None:
    """With ShellMode.OFF, an LLM call to shell_run fails because the
    tool is not in the agent's pack — no subprocess.run runs. The session
    itself still finishes."""
    workspace = _make_workspace(tmp_path)
    shell_responses = [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="s-1",
                    tool_name="Bash",
                    arguments={"command": "pytest -q"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="couldn't run")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-2"},
        ),
    ]
    host, driver = _session(
        workspace, responses=shell_responses,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.OFF,
    )
    out = driver.start(goal="run tests", agent="main")
    result = session_result(host, out)
    assert result.status == "terminal"
    # No successful shell call → no last_shell in the summary.
    assert result.last_shell is None
    # No edits either.
    assert result.files_changed == ()


def test_runner_agent_allowed_tools_filters_pack(tmp_path: Path) -> None:
    """An agent that does NOT allow `edit` cannot use it even
    if the workspace + write_mode would otherwise allow editing."""
    workspace = _make_workspace(tmp_path)
    target = workspace / "x.py"
    original = target.read_bytes()
    # The "explore" agent is read-only — no replacement tools are allowed.
    host, driver = _session(
        workspace, responses=_replace_then_finish(), agent="explore",
        write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
    )
    out = driver.start(goal="should not edit", agent="explore")
    result = session_result(host, out)
    assert result.status == "terminal"
    # Even with APPLY, the file is untouched because the tool is
    # filtered out by allowed_tools BEFORE the Engine sees it.
    assert target.read_bytes() == original
    assert result.files_changed == ()


# ---------------------------------------------------------------------------
# Durable storage + observability
# ---------------------------------------------------------------------------


def test_runner_uses_sqlite_path_when_provided(tmp_path: Path) -> None:
    """A session backed by a sqlite triple (``HostConfig``-style durable storage)
    opens the file, drives the loop, and lands the edit against it."""
    workspace = _make_workspace(tmp_path)
    db = tmp_path / "session.db"
    host, driver = _session(
        workspace, responses=_replace_then_finish(),
        write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
        sqlite_path=str(db),
    )
    out = driver.start(goal="rename foo to bar", agent="main")
    result = session_result(host, out)
    assert result.status == "terminal"
    assert db.exists() and db.stat().st_size > 0
    # The edit landed against the sqlite-backed run.
    assert (workspace / "x.py").read_text() == "bar\n"


def test_runner_sse_broadcaster_wired(tmp_path: Path) -> None:
    """An ``EventFanout`` subscribed on the host event log (the SDK observer
    extension seam) fans every committed envelope out to an
    ``EnvelopeBroadcaster`` — verified by draining the subscription."""
    from noeta.observers.fanout import EnvelopeBroadcaster, EventFanout

    broadcaster = EnvelopeBroadcaster()
    subscription = broadcaster.subscribe()
    try:
        workspace = _make_workspace(tmp_path)
        host, driver = _session(
            workspace, responses=_replace_then_finish(),
            write_mode=FsWriteMode.APPLY, shell_mode=ShellMode.OFF,
        )
        EventFanout(event_log=host.event_log, broadcaster=broadcaster)
        out = driver.start(goal="rename foo to bar", agent="main")
        assert out.status == "terminal"
        received: list[str] = []
        # Drain non-blocking by using a short timeout — every event has
        # already been published before we get here.
        while True:
            env = subscription.get(timeout=0.05)
            if env is None:
                break
            received.append(getattr(env, "type", "unknown"))
    finally:
        subscription.close()
    assert "TaskCreated" in received
    assert "TaskCompleted" in received


def test_runner_summary_captures_last_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM scripts a successful shell_run, the projected summary
    surfaces a compact `last_shell`."""
    import subprocess

    # Patch `subprocess.run` (the default ShellRunTool runner) to a
    # deterministic stub — keeps the test off the real `git` binary
    # but still exercises the shell branch of `_last_shell_result`.
    def fake_subprocess_run(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b" M a.txt\n", stderr=b""
        )

    monkeypatch.setattr(
        "noeta.runtime.subproc._default_run", fake_subprocess_run
    )

    workspace_path = _make_workspace(tmp_path)
    responses = [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="s-1",
                    tool_name="Bash",
                    arguments={"command": "git status --short"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="checked")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-2"},
        ),
    ]
    host, driver = _session(
        workspace_path, responses=responses,
        write_mode=FsWriteMode.DRY_RUN, shell_mode=ShellMode.ALLOWLIST,
    )
    out = driver.start(goal="git status", agent="main")
    result = session_result(host, out)
    assert result.status == "terminal"
    assert result.last_shell is not None
    assert result.last_shell["tool"] == "Bash"
    assert result.last_shell["returncode"] == 0
