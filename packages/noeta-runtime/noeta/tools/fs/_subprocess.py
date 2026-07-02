"""Restricted-subprocess primitives shared by ``shell_run`` /
``run_skill_script``.

The output-capping + spawn primitives are identical across the two exec
tools (``shell.py`` and ``skill_script.py``); they live here so both reuse
the *exact* timeout / truncation boundary conditions. The two tools'
*result builders* (their inline ``output`` shapes — ``returncode`` vs
``exit_code``, the skill metadata, the summary line) differ and stay in
their own modules; only the lower-level run + capture machinery is shared.

Honest boundary (B19, inherited by both callers): these primitives spawn
with a scrubbed env / bounded timeout / output cap, but do **not** sandbox
the spawned process — it can do arbitrary local IO. Trusted-workspace only.
"""

from __future__ import annotations

import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.runtime._proc_group import send_group_signal
from noeta.tools._env import scrub_env


__all__ = [
    "_RunOutcome",
    "cap_stream",
    "run_argv",
    "tail_bytes",
]


#: Module-local alias — the implementation now lives in ``noeta.tools._env``.
_scrub_env = scrub_env

#: SIGTERM → grace → SIGKILL escalation window on timeout, mirroring the
#: background shell's ``DEFAULT_KILL_GRACE_S``.
_KILL_GRACE_S = 5.0


def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
    """SIGTERM the child's whole process group, grace, then SIGKILL it.

    The child was spawned with ``start_new_session=True`` so it leads its
    own group; signalling the GROUP reaps backgrounded grandchildren
    (``bash -c "server & wait"``) that a single-PID kill would orphan.
    Both sends route through the shared :func:`send_group_signal` primitive
    (group-first, single-PID fallback, swallowing the exit race) — the same
    discipline as ``background_shell._terminate``. The trailing group SIGKILL
    is unconditional: a grandchild that traps/ignores SIGTERM must still die
    even when the direct child exited within the grace.
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
) -> "subprocess.CompletedProcess[bytes]":
    """``subprocess.run``-shaped default runner that reaps the WHOLE process
    group on timeout (tools m4).

    ``subprocess.run`` kills only the DIRECT child on timeout, orphaning
    backgrounded grandchildren. This spawns with ``start_new_session=True``
    (the child leads a fresh process group) and, on timeout, escalates
    SIGTERM → grace → SIGKILL against the group before re-raising the same
    ``TimeoutExpired`` (with whatever output was captured) that
    ``subprocess.run`` raises — so ``run_argv``'s except branch is unchanged.
    Tests that used to monkeypatch ``subprocess.run`` patch THIS seam
    (``noeta.tools.fs._subprocess._default_run``) instead.
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
        start_new_session=True,
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Everything in the group is dead now, so the pipes close and the
        # drain returns promptly with whatever output was produced.
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            argv, timeout or 0.0, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


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
class _RunOutcome:
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
) -> _RunOutcome:
    """Spawn ``argv``, capture output, enforce timeout + scrubbed env.

    ``runner`` is injectable so tests don't shell out for happy-path
    coverage; the default is :func:`_default_run` — a ``subprocess.run``-
    shaped runner that spawns the child as a process-GROUP leader and, on
    timeout, reaps the whole group (SIGTERM → grace → SIGKILL), so a
    command that backgrounds grandchildren (``bash -c "server & wait"``)
    can no longer orphan them (tools m4).
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
    return _RunOutcome(
        returncode=returncode,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
    )
