"""Background shell spawn + growing artifact (pull half).

Walks the whole background-execution spine for the FIRST slice only: the
``ProcessRegistry`` accelerator + the ``shell_run(background=true)`` /
``shell_poll`` tools + the three ``BackgroundShell*`` events, but NOT the
completion wake (02), kill (03), session re-keying (04) or read-model (05).

Coverage matrix:

* ``shell_run(background=true)`` returns ``{job_id, status:"running", ref}``
  immediately (the engine main loop is never blocked on the process), and
  ``background=false`` stays byte-for-byte the synchronous 120s-cap path.
* the watcher thread fills an off-ledger buffer; ``content_store.put`` mints
  a fresh content-addressed snapshot ref on spawn / each poll / exit, so a
  ``deref`` walks partial → complete output. Bytes NEVER inline in events.
* the three events land on the ``spawned_by_task_id`` stream via
  ``system_emit`` — ``BackgroundShellStarted`` (pid + ref),
  ``BackgroundShellPolled`` (ref + offset), ``BackgroundShellExited``
  (exit_code + final_ref + summary).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import ProcessRegistry
from noeta.protocols.decisions import ToolCall
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.tools.fs import ShellMode, ShellPollTool, ShellRunTool, WorkspaceRoot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path) -> tuple[ProcessRegistry, InMemoryEventLog, InMemoryContentStore]:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store)
    return reg, log, store


def _await_exit(reg: ProcessRegistry, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    """Poll until the watcher reaps the process (deterministic test drain)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = reg.poll(job_id)
        if status["status"] == "exited":
            return status
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not exit within {timeout_s}s")


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ---------------------------------------------------------------------------
# ProcessRegistry — spawn / watcher / poll / events
# ---------------------------------------------------------------------------


def test_spawn_returns_job_id_and_ref_without_blocking(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    # A process that sleeps before printing — spawn must return immediately,
    # well before any output exists.
    start = time.monotonic()
    out = reg.spawn(
        argv=_py("import time; time.sleep(0.3); print('done')"),
        cwd=tmp_path,
        env={},
        command="python sleeper",
        spawned_by_task_id="t-1",
        trace_id="tr-1",
    )
    elapsed = time.monotonic() - start
    assert elapsed < 0.3, "spawn blocked on the process"
    assert "job_id" in out
    # The spawn ref is a content-addressed empty snapshot.
    assert out["ref"]["size"] == 0
    _await_exit(reg, out["job_id"])


def test_output_grows_partial_then_complete(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    # Emit two lines with a gap so a mid-run poll can catch a partial prefix.
    out = reg.spawn(
        argv=_py(
            "import sys,time;"
            "sys.stdout.write('one\\n');sys.stdout.flush();"
            "time.sleep(0.4);"
            "sys.stdout.write('two\\n');sys.stdout.flush()"
        ),
        cwd=tmp_path,
        env={},
        command="python two-liner",
        spawned_by_task_id="t-1",
        trace_id="tr-1",
    )
    job_id = out["job_id"]
    # Poll until we see the first line but (ideally) not the second.
    partial_seen = False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        p = reg.poll(job_id)
        body = store.get(_ref_from_json(p["ref"]))
        if b"one" in body and b"two" not in body:
            partial_seen = True
            break
        if b"two" in body:
            break
        time.sleep(0.02)
    final = _await_exit(reg, job_id)
    final_body = store.get(_ref_from_json(final["ref"]))
    assert b"one" in final_body and b"two" in final_body
    # We at least observed monotonically-growing snapshots (partial may race
    # on a heavily-loaded box; the deterministic pull-progress guarantee is
    # asserted by ``test_poll_sees_output_while_process_still_running``).
    assert final["exit_code"] == 0
    _ = partial_seen


def test_poll_sees_output_while_process_still_running(tmp_path: Path) -> None:
    """Pull half, deterministic regression guard.

    A process writes one flushed line then BLOCKS in a long sleep (it does NOT
    exit, and emits < 4 KB). A mid-run poll MUST see that line while the job is
    still ``running``. This only holds because the watcher uses ``read1``
    (returns whatever bytes are available) — a plain ``read(_READ_CHUNK)`` would
    block until 4096 bytes OR EOF, so the buffer would stay empty for the whole
    sleep and the model could never "deref to watch progress". The earlier
    two-line test left this best-effort (it raced on the watcher-thread
    schedule); this one is deterministic because the line is available for
    seconds before any exit.
    """
    reg, _log, store = _registry(tmp_path)
    out = reg.spawn(
        argv=_py(
            "import sys,time;"
            "sys.stdout.write('progress-line\\n');sys.stdout.flush();"
            "time.sleep(30)"
        ),
        cwd=tmp_path,
        env={},
        command="python slow-emitter",
        spawned_by_task_id="t-prog",
        trace_id="tr-prog",
    )
    job_id = out["job_id"]
    try:
        body = b""
        status = "running"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            p = reg.poll(job_id)
            body = store.get(_ref_from_json(p["ref"]))
            if b"progress-line" in body:
                status = p["status"]
                break
            time.sleep(0.02)
        assert b"progress-line" in body, (
            "mid-run poll never observed output while the process was still "
            "alive — the D3 pull-progress half is broken (watcher over-buffering "
            "with read() instead of read1()?)"
        )
        assert status == "running", "output must be visible BEFORE the job exits"
    finally:
        # Reap the 30s sleeper so it never lingers past the test.
        reg.kill(job_id)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if reg.poll(job_id)["status"] in ("exited", "killed"):
                break
            time.sleep(0.02)


def test_events_carry_refs_never_inline_bytes(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    out = reg.spawn(
        argv=_py("print('hello background')"),
        cwd=tmp_path,
        env={},
        command="python hello",
        spawned_by_task_id="t-evt",
        trace_id="tr-evt",
    )
    job_id = out["job_id"]
    reg.poll(job_id)
    _await_exit(reg, job_id)
    # Drain a final poll so the polled event is present too.
    reg.poll(job_id)

    types = [e.type for e in log.read("t-evt")]
    assert "BackgroundShellStarted" in types
    assert "BackgroundShellPolled" in types
    assert "BackgroundShellExited" in types

    started = next(e for e in log.read("t-evt") if e.type == "BackgroundShellStarted")
    assert started.payload.job_id == job_id
    assert started.payload.spawned_by_task_id == "t-evt"
    assert started.payload.pid > 0
    assert started.payload.ref.size == 0  # empty snapshot at spawn
    # lifecycle events ride origin="observer" (matches the
    # ChildLifecycleObserver precedent) so the post-wake resume segment
    # re-injects an Exited that lands while the session is suspended.
    assert started.origin == "observer"

    exited = next(e for e in log.read("t-evt") if e.type == "BackgroundShellExited")
    assert exited.payload.exit_code == 0
    body = store.get(exited.payload.final_ref)
    assert b"hello background" in body
    # The bytes live in ContentStore, only the ref rides the event.
    assert isinstance(exited.payload.final_ref.hash, str)


def test_polled_event_pins_offset_snapshot(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    out = reg.spawn(
        argv=_py("print('snapshot me')"),
        cwd=tmp_path,
        env={},
        command="python snap",
        spawned_by_task_id="t-poll",
        trace_id="tr-poll",
    )
    job_id = out["job_id"]
    _await_exit(reg, job_id)
    p = reg.poll(job_id)
    assert p["status"] == "exited"
    assert "ref" in p and "offset" in p
    polled = [e for e in log.read("t-poll") if e.type == "BackgroundShellPolled"]
    assert polled, "poll must record a BackgroundShellPolled event"
    last = polled[-1]
    assert last.payload.offset == _ref_from_json(p["ref"]).size
    # Replay reads THAT ref → reproduces the exact prefix the model saw.
    assert store.get(last.payload.ref) == store.get(_ref_from_json(p["ref"]))


# ---------------------------------------------------------------------------
# Tools — shell_run(background) / shell_poll
# ---------------------------------------------------------------------------


def _ws(tmp_path: Path) -> WorkspaceRoot:
    ws = tmp_path / "ws"
    ws.mkdir()
    return WorkspaceRoot.from_path(ws)


def test_shell_run_background_true_returns_handle(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    ws = _ws(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(
        artifact_store=store,
        background_runner=reg,
        metadata={"task_id": "t-tool", "trace_id": "tr-tool"},
    )
    result = tool.invoke({"command": "printf hi", "run_in_background": True}, ctx)
    assert result.success
    assert result.output["status"] == "running"
    assert "job_id" in result.output
    assert "ref" in result.output
    _await_exit(reg, result.output["job_id"])


def test_shell_run_background_without_runner_errors_cleanly(tmp_path: Path) -> None:
    _, _, store = _registry(tmp_path)
    ws = _ws(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(artifact_store=store)  # no background_runner
    result = tool.invoke({"command": "printf hi", "run_in_background": True}, ctx)
    assert not result.success
    assert "background" in result.summary.lower()


def test_shell_run_background_false_unchanged(tmp_path: Path) -> None:
    """The synchronous path is byte-for-byte the old behaviour."""
    _, _, store = _registry(tmp_path)
    ws = _ws(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(artifact_store=store)
    result = tool.invoke({"command": "printf hi"}, ctx)
    assert result.success
    assert result.output["returncode"] == 0
    assert "stdout_tail" in result.output  # the sync result shape
    assert "job_id" not in result.output


def test_shell_run_background_reuses_mode_gate(tmp_path: Path) -> None:
    # the background path goes through the same mode gate as the sync
    # path. In the strict ALLOWLIST tier a metachar command is rejected before
    # any spawn. (In ARBITRARY it would now run through bash — full-bash
    # coverage lives in test_fs_shell_tools.test_arbitrary_mode_runs_through_bash.)
    reg, _, store = _registry(tmp_path)
    ws = _ws(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ALLOWLIST)
    ctx = ToolContext(
        artifact_store=store,
        background_runner=reg,
        metadata={"task_id": "t-tool", "trace_id": "tr-tool"},
    )
    result = tool.invoke({"command": "pytest | grep x", "run_in_background": True}, ctx)
    assert not result.success  # metachar rejected before any spawn


def test_shell_poll_tool_returns_status_ref(tmp_path: Path) -> None:
    reg, log, store = _registry(tmp_path)
    ws = _ws(tmp_path)
    run = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    poll = ShellPollTool()
    ctx = ToolContext(
        artifact_store=store,
        background_runner=reg,
        metadata={"task_id": "t-poll-tool", "trace_id": "tr"},
    )
    started = run.invoke({"command": "printf hi", "run_in_background": True}, ctx)
    job_id = started.output["job_id"]
    _await_exit(reg, job_id)
    result = poll.invoke({"job_id": job_id}, ctx)
    assert result.success
    assert result.output["status"] == "exited"
    assert "ref" in result.output
    assert result.output["exit_code"] == 0


def test_shell_poll_low_risk(tmp_path: Path) -> None:
    poll = ShellPollTool()
    assert poll.risk_level == "low"
    run = ShellRunTool(workspace=_ws(tmp_path))


# ---------------------------------------------------------------------------
# Small util — JSON ref ↔ ContentRef
# ---------------------------------------------------------------------------


def _ref_from_json(j: dict[str, Any]):
    from noeta.protocols.values import ContentRef

    return ContentRef(hash=j["hash"], size=j["size"], media_type=j["media_type"])
