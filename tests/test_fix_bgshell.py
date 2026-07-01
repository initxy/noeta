"""Regression tests for background_shell.py review fixes.

* #28 — the per-session concurrency cap must hold ONE lock across the
  count-check AND the table insert, so concurrent spawns on one session can
  never overrun ``max_jobs_per_session`` (the old code read the count under the
  lock, released it, then inserted under a separate later lock → two racing
  spawns at cap-1 could both pass the ceiling).
* #10 — ``poll`` snapshots ``status`` + ``exit_code`` together under the per-job
  lock, so a reap landing mid-poll can never yield an inconsistent pair (a
  'running' job that already carries an ``exit_code``).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _spawn_blocker(reg: ProcessRegistry, tmp_path: Path, task_id: str = "t-1") -> dict[str, Any]:
    # ``time.sleep`` (not ``sys.stdin.read()``): under pytest the inherited stdin
    # is closed, so a stdin-reading child would exit immediately and free its
    # budget mid-race — making the cap assertion flaky. A long sleep stays
    # reliably RUNNING until we kill it.
    return reg.spawn(
        argv=_py("import time; time.sleep(60)"),
        cwd=tmp_path,
        env={},
        command="python blocker",
        spawned_by_task_id=task_id,
        trace_id="tr",
    )


def _await_terminal(reg: ProcessRegistry, job_id: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if reg.poll(job_id)["status"] in ("exited", "killed"):
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


def test_concurrent_spawns_never_overrun_cap(tmp_path: Path) -> None:
    """#28 — N threads racing ``spawn`` on ONE session at cap-1 must never
    leave more than ``cap`` jobs RUNNING. Pre-fix the count-check and the insert
    lived in two separate lock regions, so racers could both pass the ceiling."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    cap = 4
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=cap)

    # Fill the session to cap-1 RUNNING jobs first.
    base = [_spawn_blocker(reg, tmp_path) for _ in range(cap - 1)]
    assert all("job_id" in j and not j.get("rejected") for j in base)

    # Now fire many concurrent spawns; at most ONE may be accepted (cap-1 → cap),
    # the rest must be rejected. Crucially the accepted count never exceeds 1.
    barrier = threading.Barrier(8)
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def racer() -> None:
        barrier.wait()  # release all threads at once to maximise the race
        out = _spawn_blocker(reg, tmp_path)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [r for r in results if not r.get("rejected")]
    # At most one new job could be admitted (cap-1 baseline + 1 = cap).
    assert len(accepted) <= 1, f"cap overrun: {len(accepted)} accepted, cap={cap}"

    # The Started events on the stream agree: never more than cap.
    started = [e for e in log.read("t-1") if e.type == "BackgroundShellStarted"]
    assert len(started) <= cap

    # Cleanup.
    for j in base + accepted:
        if "job_id" in j:
            reg.kill(j["job_id"])
            _await_terminal(reg, j["job_id"])


def test_poll_status_exit_code_pair_is_consistent(tmp_path: Path) -> None:
    """#10 — a terminal poll never reports a 'running' status alongside an
    ``exit_code``; the pair is snapshotted together under the per-job lock."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store)
    out = reg.spawn(
        argv=_py("print('hi')"),
        cwd=tmp_path,
        env={},
        command="python quick",
        spawned_by_task_id="t-poll",
        trace_id="tr",
    )
    _await_terminal(reg, out["job_id"])

    # Hammer poll while the job is terminal — every snapshot must be internally
    # consistent: status running ⇒ no exit_code; status terminal ⇒ exit_code set.
    for _ in range(50):
        p = reg.poll(out["job_id"])
        if p["status"] == "running":
            assert "exit_code" not in p
        else:
            assert p["status"] in ("exited", "killed")
            assert "exit_code" in p
