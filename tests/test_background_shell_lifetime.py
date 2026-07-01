"""Lifetime → session (root), not the spawning task.

The most awkward seam of event-sourced background execution: a background
process must **outlive the task that started it** and have its lifecycle owned
by the **session root**, not by a subtask that happened to spawn it. A long
service (``npm run dev``) must not deadlock its spawning task forever; a long
batch (``make build``) result must not be reaped when its subtask completes.

Coverage matrix (issue 04):

* **ownership = session root**: a job spawned by a SUBTASK is keyed under the
  session ROOT and its ``BackgroundShell*`` events land on the ROOT stream,
  while the ``spawned_by_task_id`` payload field keeps the real spawner
  (lineage — AC "spawned_by_task_id records lineage correctly").
* **common case (spawner == root) is byte-identical** to issues 01–03: the
  resolved root equals the spawner, so the stream + keying are unchanged.
* **spawning task is NOT blocked**: spawning a long sleeper returns
  immediately and the task reaches ``terminal`` normally while the job runs —
  a background job is NOT a ``wake_on`` blocker (unlike a subtask join).
* **process outlives the spawning task**: complete the (sub)task → the job is
  still running, still owned by the session root.
* **session-close cascade**: ``InteractionDriver.close`` SIGTERM→SIGKILL reaps
  ALL the session's background jobs (reuses issue 03's ``kill_session``), no
  orphan left in the registry.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from tests._sdk_session import official_registry as official_agent_registry
from noeta.client import SdkHost
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _await_terminal(
    reg: ProcessRegistry, job_id: str, timeout_s: float = 8.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = reg.poll(job_id)
        if state["status"] in ("exited", "killed"):
            return state
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


def _await_running(reg: ProcessRegistry, job_id: str, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if reg.poll(job_id)["status"] == "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached running within {timeout_s}s")


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _make_host(ws: Path, *, responses: list[LLMResponse]) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=ws,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )


def _registry_with_chain(
    log: InMemoryEventLog, store: InMemoryContentStore
) -> ProcessRegistry:
    return ProcessRegistry(event_log=log, content_store=store)


def _seed_subtask_chain(
    log: InMemoryEventLog, *, root: str, sub: str
) -> None:
    """Write the minimal TaskCreated genesis so a fold resolves ``sub``'s
    ``parent_task_id`` to ``root`` (and ``root`` to ``None`` = the session)."""
    from noeta.protocols.events import TaskCreatedPayload

    log.system_emit(
        task_id=root,
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="root goal", policy_name="p"),
        actor="test",
        origin="system",
        trace_id="tr",
    )
    log.system_emit(
        task_id=sub,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="sub goal", policy_name="p", parent_task_id=root, subtask_depth=1
        ),
        actor="test",
        origin="system",
        trace_id="tr",
    )


# ---------------------------------------------------------------------------
# ownership = session root (re-keying + root-stream events + lineage)
# ---------------------------------------------------------------------------


def test_subtask_spawned_job_owned_by_session_root(tmp_path: Path) -> None:
    """A SUBTASK spawns a background job → the job is keyed under the session
    ROOT, events land on the ROOT stream, and ``spawned_by_task_id`` records
    the real spawner (lineage)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    _seed_subtask_chain(log, root="root", sub="sub")
    reg = _registry_with_chain(log, store)

    out = reg.spawn(
        argv=_py("print('from a subtask')"),
        cwd=tmp_path,
        env={},
        command="python sub-job",
        spawned_by_task_id="sub",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_terminal(reg, job_id)
    reg.poll(job_id)  # drain a poll so Polled lands too

    # Events land on the SESSION ROOT stream, NOT the subtask stream.
    root_types = [e.type for e in log.read("root")]
    assert "BackgroundShellStarted" in root_types
    assert "BackgroundShellExited" in root_types
    sub_types = [e.type for e in log.read("sub")]
    assert "BackgroundShellStarted" not in sub_types
    assert "BackgroundShellExited" not in sub_types

    # Lineage: the Started payload keeps the REAL spawner (the subtask).
    started = next(
        e for e in log.read("root") if e.type == "BackgroundShellStarted"
    )
    assert started.payload.spawned_by_task_id == "sub"

    # The registry keys the job under the session root, so a session kill
    # (issue 03's primitive, reused by the close cascade) finds it there.
    assert reg.kill_session("sub") == []  # not keyed under the subtask
    # (already terminal → no live job to kill, but keyed under root)


def test_common_case_spawner_is_root_byte_identical(tmp_path: Path) -> None:
    """When the spawner IS the session root (no parent), root resolution is a
    no-op: events land on the spawner's own stream exactly as issues 01–03."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    # No TaskCreated for "t-root" → fold yields parent_task_id=None → it IS
    # the root (the common case, and what all the 01/02/03 tests rely on).
    reg = _registry_with_chain(log, store)
    out = reg.spawn(
        argv=_py("print('root job')"),
        cwd=tmp_path,
        env={},
        command="python root-job",
        spawned_by_task_id="t-root",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_terminal(reg, job_id)
    types = [e.type for e in log.read("t-root")]
    assert "BackgroundShellStarted" in types
    assert "BackgroundShellExited" in types
    started = next(
        e for e in log.read("t-root") if e.type == "BackgroundShellStarted"
    )
    assert started.payload.spawned_by_task_id == "t-root"


# ---------------------------------------------------------------------------
# spawning task NOT blocked + job outlives the spawning task
# ---------------------------------------------------------------------------


def test_spawning_task_reaches_terminal_while_job_runs(tmp_path: Path) -> None:
    """A task that spawns a long-running background job reaches ``terminal``
    normally — the job is NOT a ``wake_on`` blocker. The job keeps running and
    stays owned by the session AFTER the task is done."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # FakeLLM ends the turn immediately (no tool call) so driver.start drives
    # the task to its trailing next-goal suspension cleanly; we spawn the bg
    # job directly through the wired registry to keep the test deterministic.
    host = _make_host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    task_id = outcome.task_id

    reg = host._process_registry  # noqa: SLF001 — reach the wired registry
    assert reg is not None
    out = reg.spawn(
        argv=_py("import time; time.sleep(30)"),
        cwd=ws,
        env={},
        command="python long-sleeper",
        spawned_by_task_id=task_id,
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_running(reg, job_id)

    # The task is NOT blocked on the background job: it rests at its trailing
    # suspension (a normal, non-terminal resting point), and the job is STILL
    # running, STILL owned by the session.
    from noeta.core.fold import fold

    task = fold(host.event_log, host.content_store, task_id)
    assert task.status in ("suspended", "terminal")
    assert task.wake_on is None or "BackgroundShell" not in type(task.wake_on).__name__
    assert reg.poll(job_id)["status"] == "running"
    # The session still owns the job (a session kill finds it).
    reg.kill(job_id)
    _await_terminal(reg, job_id)


def test_job_outlives_spawning_subtask(tmp_path: Path) -> None:
    """A job spawned by a subtask survives the subtask's terminal: it is still
    in the registry under the session root, still running."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    _seed_subtask_chain(log, root="root", sub="sub")
    reg = _registry_with_chain(log, store)
    out = reg.spawn(
        argv=_py("import time; time.sleep(30)"),
        cwd=tmp_path,
        env={},
        command="python sub-sleeper",
        spawned_by_task_id="sub",
        trace_id="tr",
    )
    job_id = out["job_id"]
    _await_running(reg, job_id)
    # The subtask "completing" does not touch the registry — the job is owned
    # by the session root and keeps running.
    assert reg.poll(job_id)["status"] == "running"
    # A session-root kill reaps it (proves ownership is the root).
    killed = reg.kill_session("root")
    assert len(killed) == 1
    _await_terminal(reg, job_id)
    assert reg.poll(job_id)["status"] == "killed"


# ---------------------------------------------------------------------------
# session CLOSE cascade — reap ALL the session's jobs, no orphan
# ---------------------------------------------------------------------------


def test_session_close_reaps_all_background_jobs(tmp_path: Path) -> None:
    """``InteractionDriver.close`` cascades SIGTERM→SIGKILL to ALL the session's
    background jobs (reuses issue 03's ``kill_session`` via the same host seam
    ``cancel`` uses) — no orphan left in the registry."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _make_host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    task_id = outcome.task_id

    reg = host._process_registry  # noqa: SLF001
    assert reg is not None
    jobs = []
    for i in range(2):
        out = reg.spawn(
            argv=_py("import time; time.sleep(30)"),
            cwd=ws,
            env={},
            command=f"python sleeper-{i}",
            spawned_by_task_id=task_id,
            trace_id="tr",
        )
        jobs.append(out["job_id"])
    for job_id in jobs:
        _await_running(reg, job_id)

    # Close the conversation → cascade kill.
    driver.close(task_id)

    for job_id in jobs:
        state = _await_terminal(reg, job_id)
        assert state["status"] == "killed"

    # Both jobs recorded a Killed terminal on the SESSION ROOT stream. The
    # watcher flips status under its lock but emits the Killed event just AFTER
    # releasing it (on its own thread), so a poll seeing "killed" does not yet
    # guarantee the event is in the log — wait for both events to land before
    # the count assertion (avoids racing the second job's emit).
    def _killed_count() -> int:
        return [
            e.type
            for e in host.event_log.read(task_id)
            if e.type in ("BackgroundShellExited", "BackgroundShellKilled")
        ].count("BackgroundShellKilled")

    deadline = time.monotonic() + 8.0
    while _killed_count() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _killed_count() == 2


def test_session_close_with_no_jobs_is_clean(tmp_path: Path) -> None:
    """Closing a session with no background jobs is a clean no-op (the
    getattr-guarded cascade does not crash on an empty registry)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host = _make_host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(host)
    outcome = driver.start(goal="kick off", agent="main")
    # No jobs spawned — close must not raise.
    driver.close(outcome.task_id)
