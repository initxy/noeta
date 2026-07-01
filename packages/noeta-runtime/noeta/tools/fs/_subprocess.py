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

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from noeta.tools._env import scrub_env


__all__ = [
    "_RunOutcome",
    "cap_stream",
    "run_argv",
    "tail_bytes",
]


#: Module-local alias — the implementation now lives in ``noeta.tools._env``.
_scrub_env = scrub_env


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
    coverage — `subprocess.run` is the default.
    """
    run = runner or subprocess.run
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
