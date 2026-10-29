"""``LocalExecEnv``: the host machine as an ``ExecEnv``.

Commands run as child processes, each leading its own process group, with
a scrubbed environment and stdin closed. On timeout or cancellation the whole
group is sent SIGTERM, given a grace period, then SIGKILL — signalling the
group is what reaps backgrounded grandchildren (``bash -c "server & wait"``)
that a single-PID kill would orphan. Captured output is bounded while it is
read, so a command printing hundreds of MB is never buffered whole.

A spawned process can do anything the host user can, unless the env was given
sandbox settings (``sandbox=``, Claude Code's ``sandbox`` object): then
``env.sandbox`` is a :class:`~noeta.backends.execenvs.sandbox.LocalSandbox`
the ``Bash`` tool runs its commands through (:mod:`noeta.toolkit.sandbox`).
File operations run in ``asyncio.to_thread``. With ``fence=`` (a task of a
tenant other than the default one), a file is read or written only after
the file actually opened (or the directory a write lands in) is checked, so
a symlink swapped after a permission check cannot redirect it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import stat as _st
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO

from noeta.backends.execenvs.sandbox import LocalSandbox
from noeta.core.execenv import FileStat, RunResult
from noeta.toolkit.local_env import scrub_env
from noeta.toolkit.sandbox import SandboxSettings, sandbox_settings

__all__ = ["Fence", "LocalExecEnv", "send_group_signal"]

#: ``(resolved path, write) -> why not``, or ``None`` when allowed.
Fence = Callable[[str, bool], "str | None"]



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

    async def drain(self, fd: int) -> None:
        """Read the pipe ``fd`` to EOF; it is closed when this ends, however."""
        os.set_blocking(fd, False)
        try:
            while True:
                try:
                    chunk = os.read(fd, _CHUNK)
                except BlockingIOError:
                    await _ready(fd, write=False)
                    continue
                if not chunk:
                    return
                self.total += len(chunk)
                self.buf += chunk
                if self.keep is not None and len(self.buf) > self.keep:
                    del self.buf[: len(self.buf) - self.keep]
        finally:
            os.close(fd)

    def result(self) -> tuple[bytes, bool]:
        truncated = self.keep is not None and self.total > self.keep
        return bytes(self.buf), truncated


async def _kill_group(proc: _Child, grace: float) -> None:
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


async def _finish_kill(proc: _Child, grace: float) -> None:
    """Run :func:`_kill_group` to completion even if the caller is cancelled
    again while it waits."""
    task = asyncio.ensure_future(_kill_group(proc, grace))
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)
    await asyncio.shield(task)


async def _ready(fd: int, *, write: bool) -> None:
    """Wait until the non-blocking ``fd`` can be read (or written)."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    if write:
        loop.add_writer(fd, wake)
    else:
        loop.add_reader(fd, wake)
    try:
        await ready
    finally:
        if write:
            loop.remove_writer(fd)
        else:
            loop.remove_reader(fd)


def _close(fds: Sequence[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


class _Child:
    """A started child, reaped by a daemon thread of its own.

    Not an asyncio subprocess: before Python 3.14, asyncio waits forever for
    a child whose setup task was cancelled before it finished — and a loop
    shutting down cancels every task at once, so a command being spawned
    then hung the shutdown. Here the start is synchronous and waiting for
    the exit can be cancelled at any point."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self.pid = popen.pid
        self._popen = popen
        loop = asyncio.get_running_loop()
        self._exited: asyncio.Future[None] = loop.create_future()
        threading.Thread(target=self._reap, args=(loop,), daemon=True,
                         name=f"noeta-reap-{self.pid}").start()

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def _reap(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            _, status = os.waitpid(self.pid, 0)
            code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:  # reaped by someone else: status unknown
            code = 255
        self._popen.returncode = code
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits
            loop.call_soon_threadsafe(self._set_exited)

    def _set_exited(self) -> None:
        if not self._exited.done():
            self._exited.set_result(None)

    async def wait(self) -> int:
        await asyncio.shield(self._exited)
        return self._popen.returncode if self._popen.returncode is not None else -1


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
    ``sandbox`` — Claude Code's ``sandbox`` settings (a mapping, or
    :class:`SandboxSettings`); when they enable it, :attr:`sandbox` confines
    the commands the shell tools run. ``temp_dir`` — a temporary directory
    of the env's own (a tenant's task: ``noeta.toolkit.paths.tenant_temp_dir``;
    by default the sandbox settings' ``tenant_temp_dir``) for its background
    jobs' logs, instead of the place this machine's tasks share.
    """

    #: Commands run on this machine (``noeta.toolkit.local_env.is_local``).
    local = True

    def __init__(
        self,
        cwd: str,
        *,
        inherit_env: Sequence[str] = (),
        kill_grace: float = KILL_GRACE_S,
        sandbox: SandboxSettings | Mapping[str, object] | None = None,
        fence: Fence | None = None,
        temp_dir: str | None = None,
    ) -> None:
        self.cwd = os.path.abspath(cwd)
        self.fence = fence
        settings = sandbox_settings(sandbox, base_dir=self.cwd)
        self.temp_dir = temp_dir or (settings.tenant_temp_dir if settings else None)
        self.inherit_env = tuple(inherit_env)
        self.kill_grace = kill_grace
        self.sandbox: LocalSandbox | None = (
            LocalSandbox(settings, self.cwd) if settings is not None and settings.enabled
            else None)

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
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        in_r, in_w = os.pipe() if stdin is not None else (None, None)
        try:
            # Started synchronously, so no cancellation lands mid-start (see
            # _Child).
            proc = _Child(subprocess.Popen(
                argv,
                cwd=self._abs(cwd) if cwd is not None else self.cwd,
                env=full_env,
                # DEVNULL rather than the host's fd 0: an inherited stdin lets
                # a command block forever on a read or eat the host's input.
                stdin=in_r if in_r is not None else subprocess.DEVNULL,
                stdout=out_w,
                stderr=err_w,
                start_new_session=True,
            ))
        except BaseException:
            _close([out_r, err_r] if in_w is None else [out_r, err_r, in_w])
            raise
        finally:
            _close([out_w, err_w] if in_r is None else [out_w, err_w, in_r])
        keep = None if output_cap is None else max(0, output_cap)
        out, err = _TailBuffer(keep), _TailBuffer(keep)
        drains: list[asyncio.Future[None]] = [
            asyncio.ensure_future(out.drain(out_r)),
            asyncio.ensure_future(err.drain(err_r)),
        ]
        if in_w is not None and stdin is not None:
            drains.append(asyncio.ensure_future(_feed(in_w, stdin)))

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
            with _open_read(target, self.fence) as fh:
                return fh.read()

        return await asyncio.to_thread(_read)

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        target = self._abs(path)

        def _read() -> bytes:
            with _open_read(target, self.fence) as fh:
                fh.seek(max(0, offset))
                return fh.read(max(0, length))

        return await asyncio.to_thread(_read)

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._abs(path)
        if self.fence is None:
            await asyncio.to_thread(_atomic_write, target, data)
        else:
            await asyncio.to_thread(_fenced_write, target, data, self.fence)

    async def stat(self, path: str) -> FileStat | None:
        target = self._abs(path)
        return await asyncio.to_thread(_stat, target)

    async def realpath(self, path: str) -> str:
        return await asyncio.to_thread(os.path.realpath, self._abs(path))


async def _feed(fd: int, data: bytes) -> None:
    """Write ``data`` to the child's stdin pipe ``fd`` and close it; a child
    that exits without reading is not an error."""
    os.set_blocking(fd, False)
    left = memoryview(data)
    try:
        while left:
            try:
                left = left[os.write(fd, left):]
            except BlockingIOError:
                await _ready(fd, write=True)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        os.close(fd)


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


def _opened_path(fd: int, fallback: str) -> str:
    """The path of the file ``fd`` has open (Linux ``/proc``, macOS
    ``F_GETPATH``); ``fallback`` resolved where neither says."""
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/self/fd/{fd}").removesuffix(" (deleted)")
    import fcntl

    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:
        with contextlib.suppress(OSError):
            raw = fcntl.fcntl(fd, getpath, bytes(1024))
            return os.fsdecode(raw.split(b"\0", 1)[0])
    return os.path.realpath(fallback)


def _refuse(fence: Fence, path: str, write: bool) -> None:
    why = fence(path, write)
    if why is not None:
        raise PermissionError(why)


def _open_read(target: str, fence: Fence | None) -> BinaryIO:
    """``target`` open for reading, refused once open when the file it
    turned out to be is fenced, or is not a regular file: opened without
    blocking, so a FIFO cannot hold the thread waiting for a writer."""
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if fence is not None:
            _refuse(fence, _opened_path(fd, target), False)
        mode = os.fstat(fd).st_mode
        if _st.S_ISDIR(mode):
            raise IsADirectoryError(f"{target} is a directory")
        if not _st.S_ISREG(mode):
            raise OSError(f"{target} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _fenced_write(target: str, data: bytes, fence: Fence) -> None:
    """:func:`_atomic_write` through the directory the file lands in,
    opened and checked first: the temp file is made and renamed relative to
    that open directory, so a link swapped in meanwhile cannot move it."""
    real = os.path.realpath(target)
    _refuse(fence, real, True)
    parent, name = os.path.dirname(real) or ".", os.path.basename(real)
    _fenced_makedirs(parent, fence)
    dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _refuse(fence, os.path.join(_opened_path(dfd, parent), name), True)
        try:
            mode = os.stat(name, dir_fd=dfd).st_mode & 0o7777
        except FileNotFoundError:
            mode = 0o666 & ~_UMASK
        tmp = f".noeta-{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), mode)
                os.fsync(fh.fileno())
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)


def _fenced_makedirs(path: str, fence: Fence) -> None:
    """``os.makedirs(path)``, each missing directory made relative to its
    parent opened and checked first, so a link swapped in meanwhile cannot
    carry the new directories out of the fence."""
    missing: list[str] = []
    head = path
    while not os.path.isdir(head):
        head, tail = os.path.split(head)
        if not tail:
            break
        missing.append(tail)
    if not missing:
        return
    fd = os.open(head, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in reversed(missing):
            _refuse(fence, os.path.join(_opened_path(fd, head), name), True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o777, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd, head = child, os.path.join(head, name)
    finally:
        os.close(fd)


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
