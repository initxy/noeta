"""``shell_kill`` + human emergency-stop.

Walks the kill half of the background-execution spine:

* ``ProcessRegistry.kill(job_id)`` terminates a live process (SIGTERM, then
  SIGKILL after a grace) WITHOUT blocking the caller thread, and the watcher's
  reap path records **exactly one** terminal event — ``BackgroundShellKilled``
  (with the reaping signal), NOT also ``BackgroundShellExited``.
* the kill-vs-natural-exit race: killing a job that is about to exit on its own
  produces exactly one terminal event + exactly one completion push (the
  ``notified`` dedup under the per-job reap lock).
* idempotent / safe on an unknown job_id or an already-exited job (clean dict,
  no crash). ``kill`` never touched on replay (the registry is absent there).
* the ``shell_kill`` tool: happy path, no-runner error, unknown job_id.
* Permission Guard gates ``shell_kill`` (``risk_level="high"``) — a policy that
  denies it blocks the call exactly as it gates ``shell_run``.
* the human emergency-stop: ``kill_session`` stops ALL of a session's jobs.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Any

from tests._sdk_session import official_registry as official_agent_registry
from noeta.client import SdkHost
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import GuardContext, ProposedToolCall, Verdict
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.tool import ToolContext
from noeta.protocols.values import ContentRef
from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import (
    FsWriteMode,
    ShellKillTool,
    ShellMode,
    ShellRunTool,
    WorkspaceRoot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _registry(
    on_background_exit: Any = None,
) -> tuple[ProcessRegistry, InMemoryEventLog, InMemoryContentStore]:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(
        event_log=log, content_store=store, on_background_exit=on_background_exit
    )
    return reg, log, store


def _await_terminal(reg: ProcessRegistry, job_id: str, timeout_s: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = reg.poll(job_id)
        if state["status"] in ("exited", "killed"):
            return state
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


def _terminal_events(log: InMemoryEventLog, task_id: str) -> list[str]:
    return [
        e.type
        for e in log.read(task_id)
        if e.type in ("BackgroundShellExited", "BackgroundShellKilled")
    ]


def _ws(tmp_path: Path) -> WorkspaceRoot:
    ws = tmp_path / "ws"
    ws.mkdir()
    return WorkspaceRoot.from_path(ws)


# ---------------------------------------------------------------------------
# ProcessRegistry.kill — SIGTERM → SIGKILL, single terminal event
# ---------------------------------------------------------------------------


def test_kill_terminates_a_long_running_process(tmp_path: Path) -> None:
    """A process that would sleep ~30s is reaped promptly by ``kill``; the
    terminal event is ``BackgroundShellKilled`` (not Exited)."""
    reg, log, _ = _registry()
    out = reg.spawn(
        argv=_py("import time; time.sleep(30)"),
        cwd=tmp_path,
        env={},
        command="python long-sleeper",
        spawned_by_task_id="t-kill",
        trace_id="tr",
    )
    job_id = out["job_id"]
    # Let the watcher get blocked on wait() first.
    time.sleep(0.1)
    start = time.monotonic()
    result = reg.kill(job_id)
    # kill returns promptly (does NOT block on the grace period / wait()).
    # The discriminating threshold is DEFAULT_KILL_GRACE_S (5s) / the 30s
    # sleep; 2s keeps that discrimination sharp while tolerating a loaded
    # CI box (thread spawn under load occasionally blew a 0.5s bound).
    assert time.monotonic() - start < 2.0
    assert result["job_id"] == job_id
    assert result["status"] in ("killing", "killed")
    state = _await_terminal(reg, job_id)
    assert state["status"] == "killed"
    assert _terminal_events(log, "t-kill") == ["BackgroundShellKilled"]
    killed = next(e for e in log.read("t-kill") if e.type == "BackgroundShellKilled")
    assert killed.payload.job_id == job_id
    assert killed.payload.signal in (signal.SIGTERM, signal.SIGKILL)
    assert killed.origin == "observer"


def test_kill_escalates_to_sigkill_when_sigterm_ignored(tmp_path: Path) -> None:
    """A process that traps + ignores SIGTERM is still reaped — the grace timer
    escalates to SIGKILL and the recorded signal is SIGKILL."""
    reg, log, _ = _registry()
    out = reg.spawn(
        argv=_py(
            "import signal, time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('armed', flush=True);"
            "time.sleep(60)"
        ),
        cwd=tmp_path,
        env={},
        command="python sigterm-ignorer",
        spawned_by_task_id="t-esc",
        trace_id="tr",
    )
    job_id = out["job_id"]
    time.sleep(0.3)  # let it install the SIGTERM handler
    reg.kill(job_id, grace_s=0.3)
    state = _await_terminal(reg, job_id)
    assert state["status"] == "killed"
    killed = next(e for e in log.read("t-esc") if e.type == "BackgroundShellKilled")
    assert killed.payload.signal == int(signal.SIGKILL)


def test_kill_unknown_job_is_clean_noop(tmp_path: Path) -> None:
    reg, _, _ = _registry()
    result = reg.kill("bg-does-not-exist")
    assert result["status"] == "unknown"
    assert result["job_id"] == "bg-does-not-exist"


def test_kill_already_exited_is_clean(tmp_path: Path) -> None:
    """Killing a job that already exited naturally is a clean no-op — the
    terminal event stays ``BackgroundShellExited`` (the natural reap won)."""
    reg, log, _ = _registry()
    out = reg.spawn(
        argv=_py("print('quick')"),
        cwd=tmp_path,
        env={},
        command="python quick",
        spawned_by_task_id="t-late",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_terminal(reg, job_id)
    # Now kill the already-dead job.
    result = reg.kill(job_id)
    assert result["status"] in ("exited", "killed")
    # The single terminal event is the natural Exited (already recorded).
    assert _terminal_events(log, "t-late") == ["BackgroundShellExited"]


def test_terminate_skips_signal_when_process_already_reaped(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """``_terminate`` must NOT ``getpgid``/``killpg`` a process whose returncode
    is already set: the pid may have been recycled onto an unrelated process
    group, and group-signalling it would hit a stranger. Mirrors
    ``Popen.send_signal``'s own returncode guard."""
    import os

    reg, _, _ = _registry()

    class _FakePopen:
        pid = 999999
        returncode = 0  # already reaped by the watcher

        def send_signal(self, sig: int) -> None:  # pragma: no cover
            raise AssertionError("send_signal called on a reaped process")

    class _FakeHandle:
        popen = _FakePopen()

    called = {"killpg": False, "getpgid": False}
    monkeypatch.setattr(
        os, "killpg", lambda *a: called.__setitem__("killpg", True)
    )
    monkeypatch.setattr(
        os, "getpgid", lambda *a: (called.__setitem__("getpgid", True), 1)[1]
    )

    reg._terminate(_FakeHandle(), 15)  # SIGTERM

    assert called == {"killpg": False, "getpgid": False}


def test_kill_vs_natural_exit_race_one_terminal_one_push(tmp_path: Path) -> None:
    """A job about to exit on its own is killed at nearly the same moment.
    Whoever wins the per-job reap lock records EXACTLY ONE terminal event and
    fires EXACTLY ONE completion push — the ``notified`` dedup forbids both a
    Killed and an Exited (or a double push)."""
    pushes: list[str] = []

    def _on_exit(session_id: str, job_id: str, summary: str, ref: ContentRef) -> None:
        pushes.append(job_id)

    for i in range(20):
        reg, log, _ = _registry(on_background_exit=_on_exit)
        before = len(pushes)
        out = reg.spawn(
            # Exits on its own almost immediately — kill races the natural reap.
            argv=_py("import time; time.sleep(0.05)"),
            cwd=tmp_path,
            env={},
            command="python racer",
            spawned_by_task_id=f"t-race-{i}",
            trace_id="tr",
        )
        job_id = out["job_id"]
        reg.kill(job_id)
        _await_terminal(reg, job_id)
        # Drain a beat so the (single) push callback fires.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(pushes) == before:
            time.sleep(0.005)
        terminals = _terminal_events(log, f"t-race-{i}")
        assert len(terminals) == 1, f"iteration {i}: {terminals}"
        assert len(pushes) - before == 1, f"iteration {i}: {pushes[before:]}"


# ---------------------------------------------------------------------------
# kill_session — human emergency-stop of ALL a session's jobs
# ---------------------------------------------------------------------------


def test_kill_session_stops_all_jobs(tmp_path: Path) -> None:
    reg, log, _ = _registry()
    job_ids = []
    for n in range(3):
        out = reg.spawn(
            argv=_py("import time; time.sleep(30)"),
            cwd=tmp_path,
            env={},
            command=f"python sleeper-{n}",
            spawned_by_task_id="sess-1",
            trace_id="tr",
        )
        job_ids.append(out["job_id"])
    time.sleep(0.1)
    killed = reg.kill_session("sess-1")
    assert set(j["job_id"] for j in killed) == set(job_ids)
    for jid in job_ids:
        state = _await_terminal(reg, jid)
        assert state["status"] == "killed"
    terminals = _terminal_events(log, "sess-1")
    assert terminals == ["BackgroundShellKilled"] * 3


def test_kill_session_unknown_is_clean_noop(tmp_path: Path) -> None:
    reg, _, _ = _registry()
    assert reg.kill_session("no-such-session") == []


# ---------------------------------------------------------------------------
# Human emergency-stop via the control plane — driver.cancel kills bg jobs
# ---------------------------------------------------------------------------


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def test_driver_cancel_kills_session_background_jobs(tmp_path: Path) -> None:
    """Human emergency-stop: a control-plane ``cancel`` (the same
    call the web UI's ``POST /tasks/{id}/cancel`` makes) kills the session's
    background shell jobs via ``SdkHost.kill_background_session`` → the registry
    ``kill_session`` primitive (so a cancelled conversation leaves no orphan
    process). issue 04's session-close cascade reuses the SAME primitive."""
    ws = tmp_path / "ws"
    ws.mkdir()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=[_end_turn("hi")]),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    task_id = outcome.task_id

    # Spawn a long-running background job attributed to this session.
    reg = host._process_registry  # noqa: SLF001 — test reaches the wired registry
    assert reg is not None
    out = reg.spawn(
        argv=_py("import time; time.sleep(30)"),
        cwd=ws,
        env={},
        command="python sleeper",
        spawned_by_task_id=task_id,
        trace_id="tr",
    )
    job_id = out["job_id"]
    time.sleep(0.1)
    assert reg.poll(job_id)["status"] == "running"

    # Human emergency-stop through the control plane.
    driver.cancel(task_id)

    state = _await_terminal(reg, job_id)
    assert state["status"] == "killed"
    terminals = _terminal_events(event_log, task_id)
    assert terminals == ["BackgroundShellKilled"]


# ---------------------------------------------------------------------------
# ShellKillTool — happy path, no-runner, unknown job_id, metadata
# ---------------------------------------------------------------------------


def test_shell_kill_tool_metadata() -> None:
    tool = ShellKillTool()
    assert tool.name == "shell_kill"
    assert tool.risk_level == "high"
    assert tool.description  # hand-written canonical description


def test_shell_kill_tool_happy_path(tmp_path: Path) -> None:
    reg, log, store = _registry()
    ws = _ws(tmp_path)
    run = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    kill = ShellKillTool()
    ctx = ToolContext(
        artifact_store=store,
        background_runner=reg,
        metadata={"task_id": "t-tool", "trace_id": "tr"},
    )
    started = run.invoke(
        # No shell metachars in the raw command (a metachar-free sleeper script).
        {"command": "sleep 30", "run_in_background": True},
        ctx,
    )
    assert started.success, started.summary
    job_id = started.output["job_id"]
    time.sleep(0.1)
    result = kill.invoke({"job_id": job_id}, ctx)
    assert result.success
    assert result.output["status"] in ("killing", "killed")
    state = _await_terminal(reg, job_id)
    assert state["status"] == "killed"


def test_shell_kill_tool_no_runner_errors(tmp_path: Path) -> None:
    _, _, store = _registry()
    kill = ShellKillTool()
    ctx = ToolContext(artifact_store=store)  # no background_runner
    result = kill.invoke({"job_id": "bg-x"}, ctx)
    assert not result.success
    assert "background" in result.summary.lower()


def test_shell_kill_tool_unknown_job(tmp_path: Path) -> None:
    reg, _, store = _registry()
    kill = ShellKillTool()
    ctx = ToolContext(artifact_store=store, background_runner=reg)
    result = kill.invoke({"job_id": "bg-nope"}, ctx)
    assert not result.success
    assert "unknown" in result.summary.lower()


def test_shell_kill_tool_requires_job_id(tmp_path: Path) -> None:
    reg, _, store = _registry()
    kill = ShellKillTool()
    ctx = ToolContext(artifact_store=store, background_runner=reg)
    result = kill.invoke({}, ctx)
    assert not result.success


# ---------------------------------------------------------------------------
# Permission Guard — shell_kill is gateable in policy
# ---------------------------------------------------------------------------


def _guard_ctx() -> GuardContext:
    return GuardContext(task_id="t")


def test_permission_guard_denies_shell_kill() -> None:
    """A policy that denylists ``shell_kill`` blocks it (it is a normal,
    name-addressable tool — same machinery as ``shell_run``)."""
    kill = ShellKillTool()
    guard = PermissionGuard(
        PermissionPolicy(denied_tools=frozenset({"shell_kill"})),
        tools={"shell_kill": kill},
    )
    action = ProposedToolCall(
        call=ToolCall(call_id="c", tool_name="shell_kill", arguments={"job_id": "bg-1"})
    )
    verdict = guard.check(action, _guard_ctx())
    assert verdict.verdict is Verdict.DENY


def test_permission_guard_risk_ceiling_blocks_shell_kill() -> None:
    """``shell_kill`` is ``risk_level="high"`` — a ``max_risk_level="medium"``
    ceiling denies it (same fail-closed scale as ``shell_run``)."""
    kill = ShellKillTool()
    guard = PermissionGuard(
        PermissionPolicy(max_risk_level="medium"),
        tools={"shell_kill": kill},
    )
    action = ProposedToolCall(
        call=ToolCall(call_id="c", tool_name="shell_kill", arguments={"job_id": "bg-1"})
    )
    verdict = guard.check(action, _guard_ctx())
    assert verdict.verdict is Verdict.DENY


def test_permission_guard_allows_shell_kill_when_permitted() -> None:
    kill = ShellKillTool()
    guard = PermissionGuard(
        PermissionPolicy(max_risk_level="high"),
        tools={"shell_kill": kill},
    )
    action = ProposedToolCall(
        call=ToolCall(call_id="c", tool_name="shell_kill", arguments={"job_id": "bg-1"})
    )
    verdict = guard.check(action, _guard_ctx())
    assert verdict.is_allow
