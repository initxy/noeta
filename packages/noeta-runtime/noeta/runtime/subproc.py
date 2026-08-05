"""Restricted-subprocess primitives shared by ``shell_run`` /
``run_skill_script``.

Both exec tools must hit the *exact* same timeout / truncation boundary
conditions, so the run + capture machinery lives here while their differing
result shapes stay in their own modules. These primitives spawn with a
scrubbed env, a bounded timeout and an output cap, but they do **not**
sandbox the spawned process — it can do arbitrary local IO, so every caller
inherits a trusted-workspace-only boundary.
"""

from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.runtime._proc_group import send_group_signal
from noeta.runtime.env import scrub_env


__all__ = [
    "RunOutcome",
    "cap_stream",
    "run_argv",
    "runner_with_spawn_hook",
    "tail_bytes",
]


_scrub_env = scrub_env

#: SIGTERM → grace → SIGKILL escalation window on timeout, matching the
#: background shell's ``DEFAULT_KILL_GRACE_S``.
_KILL_GRACE_S = 5.0


def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
    """SIGTERM the child's whole process group, grace, then SIGKILL it.

    The child leads its own group, and signalling the GROUP is what reaps
    backgrounded grandchildren (``bash -c "server & wait"``) a single-PID
    kill would orphan. The trailing group SIGKILL is unconditional: a
    grandchild that traps or ignores SIGTERM must still die even when the
    direct child exited within the grace.
    """
    send_group_signal(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    send_group_signal(proc.pid, signal.SIGKILL)


def _default_run(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    capture_output: bool = True,
    timeout: Optional[float] = None,
    check: bool = False,
    on_spawn: Optional[Callable[["subprocess.Popen[bytes]"], None]] = None,
) -> "subprocess.CompletedProcess[bytes]":
    """``subprocess.run``-shaped default runner that reaps the WHOLE process
    group on timeout.

    ``subprocess.run`` kills only the DIRECT child on timeout, orphaning
    backgrounded grandchildren. This spawns the child as a group leader and
    escalates SIGTERM → grace → SIGKILL against the group, then re-raises the
    same ``TimeoutExpired`` (carrying whatever output was captured) that
    ``subprocess.run`` would, so callers need no separate timeout branch.

    ``on_spawn`` is called with the just-spawned ``Popen`` BEFORE the blocking
    wait — the seam ``shell_run`` uses to register the group in the session
    kill table so a human stop can reap it mid-run. A hook that raises must
    not leak a running child, so the group is reaped before re-raising.
    """
    del check  # parity with the subprocess.run call shape; never used here
    kwargs: dict[str, Any] = {}
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        # DEVNULL rather than the host's fd 0: an inherited stdin lets a
        # spawned command block forever on a read (burning the whole timeout)
        # or consume bytes meant for whatever drives the host process.
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        **kwargs,
    )
    if on_spawn is not None:
        try:
            on_spawn(proc)
        except BaseException:
            _kill_process_group(proc)
            raise
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # The whole group is dead, so the pipes close and this second drain
        # returns promptly with whatever output was produced.
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            argv, timeout or 0.0, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def runner_with_spawn_hook(
    on_spawn: Callable[["subprocess.Popen[bytes]"], None],
) -> Callable[..., "subprocess.CompletedProcess[bytes]"]:
    """The default runner, plus an ``on_spawn`` callback on the fresh ``Popen``.

    Lets a caller of :func:`run_argv` observe the spawned process (register its
    group in a kill table) without changing the runner seam's
    ``subprocess.run`` shape — the hook rides inside the returned runner."""

    def _run(
        argv: list[str], **kwargs: Any
    ) -> "subprocess.CompletedProcess[bytes]":
        return _default_run(argv, on_spawn=on_spawn, **kwargs)

    return _run


def tail_bytes(buf: bytes, n: int) -> tuple[str, bool]:
    """Return (utf-8 decoded tail, was_truncated)."""
    truncated = len(buf) > n
    if truncated:
        buf = buf[-n:]
    return buf.decode("utf-8", errors="replace"), truncated


def cap_stream(buf: bytes, cap: int) -> tuple[bytes, bool]:
    """Truncate ``buf`` to ``cap`` bytes; tail is what survives (so
    the agent sees the bottom of e.g. a pytest run)."""
    if len(buf) <= cap:
        return buf, False
    return buf[-cap:], True


@dataclass
class RunOutcome:
    returncode: int
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    output_cap: int,
    runner: Optional[Callable[..., subprocess.CompletedProcess[bytes]]] = None,
) -> RunOutcome:
    """Spawn ``argv``, capture output, enforce timeout + scrubbed env.

    ``runner`` is injectable so tests need not shell out for happy-path
    coverage; the default :func:`_default_run` is what makes a timeout reap
    the child's whole process group.
    """
    run = runner or _default_run
    env = _scrub_env()
    start = time.monotonic()
    timed_out = False
    try:
        proc = run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        stdout = proc.stdout or b""
        stderr = proc.stderr or b""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        returncode = -1
        timed_out = True
    duration_ms = int((time.monotonic() - start) * 1000)
    stdout, stdout_truncated = cap_stream(stdout, output_cap)
    stderr, stderr_truncated = cap_stream(stderr, output_cap)
    return RunOutcome(
        returncode=returncode,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
    )
