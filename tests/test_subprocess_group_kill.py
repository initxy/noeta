"""tools m4 — subprocess process-group kill on timeout.

``run_argv`` used to delegate to ``subprocess.run``, whose timeout kills
only the DIRECT child: a command that backgrounds grandchildren
(``bash -c "server & wait"``) orphaned them. The default runner is now
Popen-based (``start_new_session=True``) and on timeout escalates
SIGTERM → grace → SIGKILL against the whole process group.

These are the first dedicated ``run_argv`` tests (real-exec, POSIX —
matching the codebase's POSIX-only process handling).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from noeta.tools.fs._subprocess import _RunOutcome, run_argv


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_run_argv_happy_path_real_exec(tmp_path: Path) -> None:
    outcome = run_argv(
        ["/bin/sh", "-c", "echo out; echo err 1>&2"],
        cwd=tmp_path,
        timeout_s=10,
        output_cap=4096,
    )
    assert isinstance(outcome, _RunOutcome)
    assert outcome.returncode == 0
    assert outcome.timed_out is False
    assert outcome.stdout == b"out\n"
    assert outcome.stderr == b"err\n"


def test_run_argv_timeout_reaps_backgrounded_grandchild(tmp_path: Path) -> None:
    """The m4 bug itself: a grandchild backgrounded by the direct child
    must not survive the timeout kill."""
    pid_file = tmp_path / "grandchild.pid"
    # The child backgrounds a long sleep (the grandchild), records its pid,
    # then blocks — guaranteeing the timeout fires while both are alive.
    script = f"sleep 300 & echo $! > {pid_file}; wait"
    start = time.monotonic()
    outcome = run_argv(
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        timeout_s=1,
        output_cap=4096,
    )
    assert outcome.timed_out is True
    assert outcome.returncode == -1
    grandchild_pid = int(pid_file.read_text().strip())
    # The group kill reaped the grandchild (allow a beat for the signal).
    deadline = time.monotonic() + 10.0
    while _alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the timeout kill (orphaned)"
    )
    # SIGTERM killed the sh promptly — the 5s SIGKILL grace was not burned.
    assert time.monotonic() - start < 8.0


def test_run_argv_timeout_preserves_partial_output(tmp_path: Path) -> None:
    outcome = run_argv(
        ["/bin/sh", "-c", "echo early; sleep 300"],
        cwd=tmp_path,
        timeout_s=1,
        output_cap=4096,
    )
    assert outcome.timed_out is True
    assert b"early" in outcome.stdout


def test_run_argv_injected_runner_contract_unchanged(tmp_path: Path) -> None:
    """The ``runner`` seam keeps the ``subprocess.run``-shaped
    ``CompletedProcess`` contract (zero churn for runner-injecting tests)."""
    calls: list[list[str]] = []

    def _runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=7, stdout=b"faked", stderr=b""
        )

    outcome = run_argv(
        ["whatever"], cwd=tmp_path, timeout_s=5, output_cap=64, runner=_runner
    )
    assert calls == [["whatever"]]
    assert outcome.returncode == 7
    assert outcome.stdout == b"faked"
    assert outcome.timed_out is False
