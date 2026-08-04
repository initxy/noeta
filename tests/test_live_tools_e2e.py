"""Real-LLM local-tool effects, end to end (live marker).

Five product-path loops through ``SdkHost`` + ``InteractionDriver`` + presets
main, proving a real model drives the fs / shell / todo tools and the effect is
observable — on disk, in the shell result, or in the folded task state. The fake
suite pins each tool's contract deterministically (``test_fs_edit_tools.py``,
``test_background_shell.py``, ``test_code_todo_write.py``); what a real model
adds is proof that it actually *chooses* the tool and threads a usable argument.

Shell runs against the real ``LocalExecEnv`` (local subprocess, no container):

1. **Bash (allowlist)** — ``ls`` is in the product's default allowlist, so an
   ``ALLOWLIST``-mode agent can list the workspace and the result round-trips.
2. **Bash (arbitrary)** — ``python -c`` is NOT allowlisted, so this needs
   ``ARBITRARY`` mode; the computed output reaches the model.
3. **Write** — a real ``FsWriteMode.APPLY`` write lands the file on disk.
4. **Edit** — an edit changes an existing file's bytes on disk with no failed edit.
5. **TodoWrite** — the control tool replace-alls ``TaskState.todos`` (no
   ToolResultRecorded — it is a durable state patch, not an executed tool).

Config comes from a git-ignored ``.env`` via ``tests._live_env``. Missing
base/key/model auto-skips; CI never runs these. Assertions watch **structural**
invariants (tool success, file on disk, todos populated), never verbatim text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.core.fold import fold
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests import _live_env
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
    session_result,
)

pytestmark = pytest.mark.live

requires_live = _live_env.requires_live


def _model() -> str:
    return _live_env.live_model() or ""


def _host(
    ws: Path,
    *,
    write_mode: FsWriteMode = FsWriteMode.DRY_RUN,
    shell_mode: ShellMode = ShellMode.OFF,
    todo_write: bool = False,
    require_approval_tools: tuple[str, ...] = (),
    permission_mode: str = "default",
    max_steps: int = 8,
):
    """A one-shot main session with the requested tool modes."""
    main = runner_main_spec("main", todo_write=todo_write)
    host = make_host(
        make_registry(main),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=write_mode,
        shell_mode=shell_mode,
        require_approval_tools=require_approval_tools,
        permission_mode=permission_mode,
        max_steps=max_steps,
    )
    return host, make_driver(host)


def _tool_results(host, task_id: str, tool_name: str) -> list:
    """The ToolResultRecorded payloads for a given tool name on this stream.

    Pairs each ToolResultRecorded back to its ToolCallStarted by call_id, since
    the result payload carries the call_id but not the tool name.
    """
    events = list(host.event_log.read(task_id))
    ids = {
        e.payload.call_id
        for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == tool_name
    }
    return [
        e.payload
        for e in events
        if e.type == "ToolResultRecorded" and e.payload.call_id in ids
    ]


# ---------------------------------------------------------------------------
# Loop 1 — Bash, allowlist mode (ls is in the default allowlist)
# ---------------------------------------------------------------------------


@requires_live
def test_live_bash_allowlist(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "readme.txt").write_text("hello\n", encoding="utf-8")
    (ws / "data.csv").write_text("a,b\n", encoding="utf-8")
    host, driver = _host(ws, shell_mode=ShellMode.ALLOWLIST)
    out = driver.start(
        goal=(
            "Use the Bash tool to run `ls` in the workspace, then reply with the "
            "names of the files you saw."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    results = _tool_results(host, out.task_id, "Bash")
    assert results, "model never ran the Bash tool"
    assert any(r.success for r in results), "no successful Bash result"
    # The shell result is projected into the session read-model.
    assert session_result(host, out).last_shell is not None


# ---------------------------------------------------------------------------
# Loop 2 — Bash, arbitrary mode (python -c is not allowlisted)
# ---------------------------------------------------------------------------


@requires_live
def test_live_bash_arbitrary(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # ``python3 -c`` is not in the allowlist, so an unbypassed session would park
    # it on the HITL approval gate (Bash is declared high-risk).
    # ``bypassPermissions`` runs it ungated — the point here is the real
    # subprocess round-trip, not the approval path (covered in the interactive
    # suite).
    host, driver = _host(
        ws, shell_mode=ShellMode.ARBITRARY, permission_mode="bypassPermissions"
    )
    out = driver.start(
        goal=(
            "Use the Bash tool to run exactly: python3 -c \"print(6*7)\" — then "
            "reply with the number it printed."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    results = _tool_results(host, out.task_id, "Bash")
    assert results, "model never ran the Bash tool"
    assert any(r.success for r in results), "no successful Bash result"
    # "42" is only knowable from the real subprocess output.
    cs = host.content_store
    outputs = "".join(
        cs.get(r.output_ref).decode("utf-8", "replace") for r in results
    )
    assert "42" in outputs, f"computed output never reached the log: {outputs!r}"


# ---------------------------------------------------------------------------
# Loop 3 — Write, apply mode (file lands on disk)
# ---------------------------------------------------------------------------


@requires_live
def test_live_write_applies_to_disk(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _host(ws, write_mode=FsWriteMode.APPLY)
    out = driver.start(
        goal=(
            "Use the Write tool to create a file named hello.txt in the workspace "
            "with the exact contents: ahoy there\n"
            "Then finish by replying exactly: written."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    written = ws / "hello.txt"
    assert written.is_file(), "model never wrote the file to disk"
    assert "ahoy" in written.read_text(encoding="utf-8").lower()
    changed = {c["path"] for c in session_result(host, out).files_changed}
    assert "hello.txt" in changed


# ---------------------------------------------------------------------------
# Loop 4 — Edit, apply mode (existing file's bytes change on disk)
# ---------------------------------------------------------------------------


@requires_live
def test_live_edit_changes_disk(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "config.txt"
    target.write_text("status = OLD\n", encoding="utf-8")
    host, driver = _host(ws, write_mode=FsWriteMode.APPLY)
    out = driver.start(
        goal=(
            "Use the Edit tool on config.txt in the workspace to replace the word "
            "OLD with the word NEW. Then reply exactly: edited."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    contents = target.read_text(encoding="utf-8")
    assert "NEW" in contents and "OLD" not in contents, contents
    assert not session_result(host, out).failed_edits, "an edit failed"


# ---------------------------------------------------------------------------
# Loop 5 — TodoWrite (durable state patch, not an executed tool)
# ---------------------------------------------------------------------------


@requires_live
def test_live_todo_write(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _host(ws, todo_write=True)
    out = driver.start(
        goal=(
            "Use the TodoWrite tool to record exactly two todos: 'draft the spec' "
            "with status in_progress, and 'write tests' with status pending. Then "
            "finish by replying exactly: tracked."
        ),
        agent="main",
    )
    assert out.status == "terminal"
    types = [e.type for e in host.event_log.read(out.task_id)]
    assert "TaskStatePatched" in types, "TodoWrite never patched task state"
    todos = fold(host.event_log, host.content_store, out.task_id).state.todos
    assert len(todos) == 2, f"expected 2 todos, got {todos}"
    statuses = {t["status"] for t in todos}
    assert statuses <= {"pending", "in_progress", "completed"}, statuses
