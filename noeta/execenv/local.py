"""``LocalExecEnv``: the host machine as an ``ExecEnv``.

Commands run as asyncio subprocesses, each leading its own process group, with
a scrubbed environment and stdin closed. On timeout or cancellation the whole
group is sent SIGTERM, given a grace period, then SIGKILL — signalling the
group is what reaps backgrounded grandchildren (``bash -c "server & wait"``)
that a single-PID kill would orphan. Captured output is bounded while it is
read, so a command printing hundreds of MB is never buffered whole.

The spawned process is **not** sandboxed: it can do anything the host user
can. File operations run in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat as _st
import tempfile
from collections.abc import Iterable, Mapping, Sequence

from noeta.execenv.base import FileStat, RunResult

__all__ = ["ENV_ALLOWLIST", "LocalExecEnv", "scrub_env", "send_group_signal"]


#: The only parent-environment variables a spawned command inherits, so a
#: child never picks up the host's credentials by accident. A host that needs
#: more names them in ``LocalExecEnv(inherit_env=...)``.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TMPDIR",
        # Python + uv interpreter discovery
        "PYTHONHASHSEED",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)

#: SIGTERM -> grace -> SIGKILL escalation window, in seconds.
KILL_GRACE_S = 5.0

#: How long to keep draining pipes after the group was killed. A process that
#: escaped the group (its own ``setsid``) can hold a pipe open forever; what it
#: wrote so far is kept and the rest is abandoned.
_DRAIN_AFTER_KILL_S = 1.0

_CHUNK = 64 * 1024

#: Races every kill path treats as benign: the process may have exited
#: between the reap and the signal, or its pid is not ours to signal.
_BENIGN = (ProcessLookupError, PermissionError, OSError, ValueError)

#: Kill tasks that must finish even if the awaiting coroutine is cancelled
#: again; held here so they are not garbage-collected mid-flight.
_REAPERS: set[asyncio.Task[None]] = set()


def _read_umask() -> int:
    # ``os.umask`` can only be read by setting it; done once, at import, so no
    # file-writing thread ever races the temporary value.
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


_UMASK = _read_umask()


def scrub_env(
    allowlist: Iterable[str] | None = None,
    *,
    inherit: Iterable[str] = (),
) -> dict[str, str]:
    """A minimal environment built from the parent's: :data:`ENV_ALLOWLIST`
    (or ``allowlist``) plus the ``inherit`` names, nothing else."""
    parent = os.environ
    keys = set(ENV_ALLOWLIST if allowlist is None else allowlist) | set(inherit)
    return {key: parent[key] for key in sorted(keys) if key in parent}


def send_group_signal(pid: int, sig: int, *, require_leader: bool = False) -> None:
    """Send ``sig`` to ``pid``'s process group, falling back to the single PID.

    ``require_leader`` group-signals only when ``pid`` leads its own group: a
    pid sitting in someone else's group falls back to a single-PID signal so
    unrelated processes are never taken down. A signal that fails because the
    process just died is swallowed."""
    try:
        pgid: int | None = os.getpgid(pid)
    except _BENIGN:
        pgid = None
    if pgid is not None and (not require_leader or pgid == pid):
        try:
            os.killpg(pgid, sig)
            return
        except _BENIGN:
            pass
    try:
        os.kill(pid, sig)
    except _BENIGN:
        pass


class _TailBuffer:
    """Drains one pipe, keeping only its last ``keep`` bytes (all when
    ``keep`` is ``None``) and counting everything that passed."""

    def __init__(self, keep: int | None) -> None:
        self.keep = keep
        self.buf = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_CHUNK)
            if not chunk:
                return
            self.total += len(chunk)
            self.buf += chunk
            if self.keep is not None and len(self.buf) > self.keep:
                del self.buf[: len(self.buf) - self.keep]

    def result(self) -> tuple[bytes, bool]:
        truncated = self.keep is not None and self.total > self.keep
        return bytes(self.buf), truncated


async def _kill_group(proc: asyncio.subprocess.Process, grace: float) -> None:
    """SIGTERM the child's group, wait up to ``grace``, then SIGKILL the group
    unconditionally — a grandchild that ignores SIGTERM must die even when
    the direct child exited within the grace."""
    send_group_signal(proc.pid, signal.SIGTERM)
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), grace)
    send_group_signal(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        await proc.wait()


async def _finish_kill(proc: asyncio.subprocess.Process, grace: float) -> None:
    """Run :func:`_kill_group` to completion even if the caller is cancelled
    again while it waits."""
    task = asyncio.ensure_future(_kill_group(proc, grace))
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)
    await asyncio.shield(task)


async def _settle(tasks: Sequence[asyncio.Future[None]], timeout: float) -> None:
    """Let the pipe drains finish for up to ``timeout``, then cancel them."""
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class LocalExecEnv:
    """The default ``ExecEnv``: the local filesystem and subprocesses.

    ``cwd`` is the workspace directory relative paths resolve against.
    ``inherit_env`` names host variables commands inherit past
    :data:`ENV_ALLOWLIST`. ``kill_grace`` is the SIGTERM -> SIGKILL window.
    """

    def __init__(
        self,
        cwd: str,
        *,
        inherit_env: Sequence[str] = (),
        kill_grace: float = KILL_GRACE_S,
    ) -> None:
        self.cwd = os.path.abspath(cwd)
        self.inherit_env = tuple(inherit_env)
        self.kill_grace = kill_grace

    def _abs(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.cwd, path)

    # -- processes ---------------------------------------------------------

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        output_cap: int | None = None,
    ) -> RunResult:
        if not argv:
            raise ValueError("argv must not be empty")
        full_env = {**scrub_env(inherit=self.inherit_env), **(env or {})}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._abs(cwd) if cwd is not None else self.cwd,
            env=full_env,
            # DEVNULL rather than the host's fd 0: an inherited stdin lets a
            # command block forever on a read or eat the host's input.
            stdin=(
                asyncio.subprocess.PIPE
                if stdin is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        keep = None if output_cap is None else max(0, output_cap)
        out, err = _TailBuffer(keep), _TailBuffer(keep)
        drains: list[asyncio.Future[None]] = [
            asyncio.ensure_future(out.drain(proc.stdout)),
            asyncio.ensure_future(err.drain(proc.stderr)),
        ]
        if stdin is not None:
            drains.append(asyncio.ensure_future(_feed(proc, stdin)))

        async def _complete() -> None:
            await asyncio.gather(*drains)
            await proc.wait()

        timed_out = False
        try:
            # The run ends at EOF on both pipes plus the child's exit; the
            # timeout covers all of it.
            await asyncio.wait_for(_complete(), timeout)
        except TimeoutError:
            timed_out = True
            await _finish_kill(proc, self.kill_grace)
            await _settle(drains, _DRAIN_AFTER_KILL_S)
        except BaseException:
            # Cancellation (or anything else): never leave the group running.
            await _finish_kill(proc, self.kill_grace)
            for task in drains:
                task.cancel()
            raise
        stdout, stdout_truncated = out.result()
        stderr, stderr_truncated = err.result()
        code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
        return RunResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    # -- files -------------------------------------------------------------

    async def read_bytes(self, path: str) -> bytes:
        target = self._abs(path)

        def _read() -> bytes:
            with open(target, "rb") as fh:
                return fh.read()

        return await asyncio.to_thread(_read)

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        target = self._abs(path)

        def _read() -> bytes:
            with open(target, "rb") as fh:
                fh.seek(max(0, offset))
                return fh.read(max(0, length))

        return await asyncio.to_thread(_read)

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._abs(path)
        await asyncio.to_thread(_atomic_write, target, data)

    async def stat(self, path: str) -> FileStat | None:
        target = self._abs(path)
        return await asyncio.to_thread(_stat, target)

    async def realpath(self, path: str) -> str:
        return await asyncio.to_thread(os.path.realpath, self._abs(path))


async def _feed(proc: asyncio.subprocess.Process, data: bytes) -> None:
    """Write ``data`` to the child's stdin and close it; a child that exits
    without reading is not an error."""
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()


def _stat(target: str) -> FileStat | None:
    try:
        lst = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return None
    is_symlink = os.path.islink(target)
    try:
        st = os.stat(target) if is_symlink else lst
    except (FileNotFoundError, NotADirectoryError):
        # A dangling symlink: the link exists, its target does not.
        st = lst
    return FileStat(
        size=st.st_size,
        mtime=st.st_mtime,
        is_dir=_st.S_ISDIR(st.st_mode),
        is_symlink=is_symlink,
    )


def _atomic_write(target: str, data: bytes) -> None:
    """Write through a temp file in the same directory and rename it into
    place. Parents are created; a symlink is written through to its target;
    an existing file keeps its permission bits."""
    real = os.path.realpath(target)
    parent = os.path.dirname(real) or "."
    os.makedirs(parent, exist_ok=True)
    try:
        mode: int | None = os.stat(real).st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=".noeta-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is None:
            mode = 0o666 & ~_UMASK
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
