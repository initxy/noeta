"""``ContainerExecEnv``: a running Docker container as an ``ExecEnv``.

Every operation is one ``docker exec`` against a container the host started
(``start_container`` is a small helper for that; provisioning policy —
naming, mounts, reuse, idle reclamation — stays the host's). Tools run inside
the container and see only what is mounted into it; the host's environment
never reaches a command (``docker exec`` starts from the container's own).

What the container needs: ``sh``, GNU coreutils (``cat``, ``head``,
``tail``, ``stat``, ``realpath``, ``mktemp``) and util-linux ``setsid`` —
every Debian/Ubuntu-based image has them, busybox/alpine does not — plus
``bash`` for the ``Bash`` tool.

Mechanics, and the pitfalls they avoid:

* **Killing the command, not just the client.** Signalling the local
  ``docker exec`` client does not stop the process in the container. Each
  command therefore runs as ``setsid -w sh -c '<record pid>; "$@"'``, its own
  process group with its pid in a per-run file; on timeout or cancellation
  the env signals that group inside the container (SIGTERM, a grace period,
  then SIGKILL), which also reaps backgrounded grandchildren. The local client
  then exits by itself, with whatever the command printed.
* **Bytes, not text.** Without a TTY, ``docker exec``'s stdout is a raw byte
  stream, so files are read with ``cat`` / ``tail | head`` and written by
  piping into ``cat`` with no encoding step. (0.6's HTTP sandbox had to
  base64 through a text endpoint and wrap its guards in a subshell so a bare
  ``exit`` could not kill its persistent shell; a fresh ``docker exec`` per
  operation has no persistent shell to wedge.)
* **The same errors as the local env.** A shell guard runs before each
  read and maps "missing", "unreadable" and "a directory" to distinct exit
  codes, raised as ``FileNotFoundError`` / ``PermissionError`` /
  ``IsADirectoryError``. A write goes through a temp file in the target's
  directory and a rename, keeps an existing file's mode and owner, and
  creates parents.
* **Bounded output.** Captured streams are bounded while they are read
  (``output_cap`` keeps the tail), as on the host.

Starting, stopping and removing containers is
:mod:`.container_provision`.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import posixpath
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import msgspec

from noeta.backends.execenvs.container_provision import (
    _PROXIES as _PROXIES,
)
from noeta.backends.execenvs.container_provision import (
    CLIENT_ENV,
    DEFAULT_CAPS,
    DEFAULT_PIDS_LIMIT,
    DOCKER_ENV,
    FILE_OP_TIMEOUT_S,
    ContainerError,
    container_state,
    ensure_container_proxy,
    remove_container,
    start_container,
    stop_container,
)
from noeta.backends.execenvs.local import KILL_GRACE_S, LocalExecEnv
from noeta.core.execenv import FileStat, RunResult

__all__ = [
    "DEFAULT_CAPS",
    "DEFAULT_PIDS_LIMIT",
    "DOCKER_ENV",
    "ContainerError",
    "ContainerExecEnv",
    "container_state",
    "ensure_container_proxy",
    "remove_container",
    "start_container",
    "stop_container",
]

#: How long to wait for the client to exit after the group was killed.
_DRAIN_S = 5.0

_NOT_FOUND, _NO_PERM, _IS_DIR = 3, 4, 5

# Record the group leader's pid, run the command, clean up. ``$1`` is the
# pid file; the command is the rest. Not ``exec``: the shell stays to remove the
# file, and a missing program exits 127 as through any shell.
_WRAP = 'f=$1; shift; echo $$ > "$f"; "$@"; s=$?; rm -f -- "$f"; exit $s'

# Signal the group named in the pid file. The file may not exist yet when a
# cancel lands right after the start, so look for it a little while.
_KILL = ('i=0; while [ ! -s "$1" ] && [ $i -lt 20 ]; do sleep 0.05; i=$((i+1)); done; '
         '[ -s "$1" ] || exit 0; kill -s "$2" -- "-$(cat -- "$1")" 2>/dev/null; '
         '[ "$2" = KILL ] && rm -f -- "$1"; exit 0')

_GUARD = (f'[ -e "$1" ] || exit {_NOT_FOUND}; [ -d "$1" ] && exit {_IS_DIR}; '
          f'[ -r "$1" ] || exit {_NO_PERM}; ')

_READ = _GUARD + 'exec cat -- "$1"'
_READ_RANGE = _GUARD + 'tail -c "+$(($2 + 1))" -- "$1" | head -c "$3"'

_WRITE = (
    't=$(realpath -m -- "$1") || exit 1; '
    f'[ -d "$t" ] && exit {_IS_DIR}; '
    'd=$(dirname -- "$t"); mkdir -p -- "$d" || exit 1; '
    'tmp=$(mktemp "$d/.noeta-XXXXXX") || exit 1; '
    'if ! cat > "$tmp"; then rm -f -- "$tmp"; exit 1; fi; '
    'if [ -e "$t" ]; then chmod --reference="$t" -- "$tmp"; '
    'chown --reference="$t" -- "$tmp" 2>/dev/null; '
    'else chmod "$(printf %o $((0666 & ~0$(umask))))" -- "$tmp"; fi; '
    'mv -f -- "$tmp" "$t" || { rm -f -- "$tmp"; exit 1; }'
)

_STAT = (
    'if [ -L "$1" ]; then l=1; else l=0; fi; '
    'if [ -e "$1" ]; then stat -L -c "%s %.9Y %F" -- "$1" || exit 1; '
    'elif [ $l = 1 ]; then stat -c "%s %.9Y %F" -- "$1" || exit 1; '
    f'else exit {_NOT_FOUND}; fi; echo $l'
)

#: ``stat_many``: each existing path's name and facts, NUL-separated; a
#: missing one is left out. GNU ``stat`` takes them all at once; one
#: without ``--printf`` (BusyBox, as on Alpine) is asked path by path.
_STAT_MANY = (
    'if stat --printf "" -- / >/dev/null 2>&1; then '
    'stat -L --printf "%n\\0%s %.9Y %F\\0" -- "$@" 2>/dev/null; '
    'else for p do s=$(stat -L -c "%s %.9Y %F" -- "$p" 2>/dev/null) '
    '&& printf "%s\\0%s\\0" "$p" "$s"; done; fi; exit 0'
)
#: Bytes of paths one ``stat_many`` command carries at most (far below the
#: kernel's argument limit).
_STAT_MANY_ARG_BYTES = 100_000


class ContainerExecEnv:
    """An ``ExecEnv`` over ``docker exec`` into a running container.

    ``container``: its name or id. ``cwd``: the workspace directory inside
    it, which relative paths resolve against (default ``/workspace``).
    ``user``: run as this container user (``docker exec -u``); default the
    image's. ``env``: variables set on every command (laid under a call's
    own ``env``). ``docker``: the CLI to run. ``kill_grace``: the SIGTERM ->
    SIGKILL window.
    """

    def __init__(
        self,
        container: str,
        *,
        cwd: str = "/workspace",
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        docker: str = "docker",
        kill_grace: float = KILL_GRACE_S,
    ) -> None:
        self.local = False
        if not posixpath.isabs(cwd):
            raise ValueError(f"cwd must be an absolute path inside the container: {cwd!r}")
        self.container = container
        self.cwd = posixpath.normpath(cwd)
        self.user = user
        self.env = dict(env or {})
        self.docker = docker
        self.kill_grace = kill_grace
        self._host = LocalExecEnv(tempfile.gettempdir(), kill_grace=kill_grace)

    def _abs(self, path: str) -> str:
        return path if posixpath.isabs(path) else posixpath.join(self.cwd, path)

    def _exec(self, argv: Sequence[str], *, cwd: str | None = None,
              env: Mapping[str, str] | None = None, stdin: bool = False
              ) -> tuple[list[str], dict[str, str]]:
        """The ``docker exec`` argv and the variables the ``docker`` client
        must carry: each variable is named with ``-e KEY`` and its value
        travels in the client's environment, never on its command line
        (where ``ps`` shows it to every user)."""
        cmd = [self.docker, "exec"]
        if stdin:
            cmd.append("-i")
        if self.user:
            cmd += ["-u", self.user]
        cmd += ["-w", self._abs(cwd) if cwd is not None else self.cwd]
        values: dict[str, str] = {}
        for key, value in {**self.env, **(env or {})}.items():
            if key in CLIENT_ENV:
                # The client itself reads these (PATH finds ``docker``,
                # DOCKER_HOST picks the daemon): set them in the container
                # only. Not secrets.
                cmd += ["-e", f"{key}={value}"]
            else:
                cmd += ["-e", key]
                values[key] = value
        return [*cmd, self.container, *argv], values

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
        pidfile = f"/tmp/.noeta-run-{uuid.uuid4().hex}.pid"
        cmd, values = self._exec(["setsid", "-w", "sh", "-c", _WRAP, "sh", pidfile, *argv],
                                 cwd=cwd, env=env, stdin=stdin is not None)
        task = asyncio.ensure_future(self._host.run(cmd, env=values, stdin=stdin,
                                                    output_cap=output_cap))
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            await self._stop(pidfile, task)
            result = await self._settle(task)
            return msgspec.structs.replace(result, exit_code=-1, timed_out=True)
        except BaseException:
            # Cancelled: the command must not outlive the call, even if this
            # coroutine is cancelled again while the kill is under way.
            stopper = asyncio.ensure_future(self._stop(pidfile, task))
            with contextlib.suppress(BaseException):
                await asyncio.shield(stopper)
            task.cancel()
            raise
        self._raise_docker_failure(result)
        return result

    async def _stop(self, pidfile: str, task: asyncio.Future[RunResult]) -> None:
        """SIGTERM the command's group in the container, give it the grace
        period, then SIGKILL it."""
        await self._signal(pidfile, "TERM")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), self.kill_grace)
        await self._signal(pidfile, "KILL")

    async def _signal(self, pidfile: str, sig: str) -> None:
        with contextlib.suppress(OSError):
            cmd, values = self._exec(["sh", "-c", _KILL, "sh", pidfile, sig])
            await self._host.run(cmd, env=values, timeout=FILE_OP_TIMEOUT_S)

    async def _settle(self, task: asyncio.Future[RunResult]) -> RunResult:
        try:
            return await asyncio.wait_for(asyncio.shield(task), _DRAIN_S)
        except TimeoutError:
            # The client is stuck (a process escaped the group and holds the
            # pipe): give up on its output.
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return RunResult(exit_code=-1, stdout=b"", timed_out=True)

    def _raise_docker_failure(self, result: RunResult) -> None:
        """``docker exec`` itself failed: no daemon or no such container
        (exit 1 or 125), or the process could not start, e.g. a missing
        working directory (126/127 with an OCI message)."""
        if result.exit_code not in (1, 125, 126, 127):
            return
        # The OCI message arrives on stdout, the daemon's on stderr.
        err = (result.stderr or result.stdout[:4096]).decode("utf-8", errors="replace").strip()
        if result.exit_code in (1, 125, 126, 127) and err.startswith(
                ("Error response from daemon", "OCI runtime", "Cannot connect to the Docker")):
            if "chdir" in err or "no such file or directory" in err.lower():
                raise FileNotFoundError(errno.ENOENT, err)
            raise ContainerError(err or f"docker exec failed ({result.exit_code})")

    # -- files -------------------------------------------------------------

    async def _sh(self, script: str, *args: str, stdin: bytes | None = None) -> RunResult:
        cmd, values = self._exec(["sh", "-c", script, "sh", *args], stdin=stdin is not None)
        result = await self._host.run(cmd, env=values, stdin=stdin, timeout=FILE_OP_TIMEOUT_S)
        if result.timed_out:
            raise ContainerError(f"docker exec timed out after {FILE_OP_TIMEOUT_S:.0f}s")
        self._raise_docker_failure(result)
        return result

    @staticmethod
    def _fail(result: RunResult, path: str) -> OSError:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        code = result.exit_code
        if code == _NOT_FOUND:
            return FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        if code == _NO_PERM or "Permission denied" in err:
            return PermissionError(errno.EACCES, "Permission denied", path)
        if code == _IS_DIR:
            return IsADirectoryError(errno.EISDIR, "Is a directory", path)
        return OSError(errno.EIO, err or f"container command failed ({code})", path)

    async def read_bytes(self, path: str) -> bytes:
        target = self._abs(path)
        result = await self._sh(_READ, target)
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        target = self._abs(path)
        if length <= 0:
            if await self.stat(target) is None:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", target)
            return b""
        result = await self._sh(_READ_RANGE, target, str(max(0, offset)), str(length))
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._abs(path)
        result = await self._sh(_WRITE, target, stdin=data)
        if result.exit_code != 0:
            raise self._fail(result, target)

    async def stat(self, path: str) -> FileStat | None:
        target = self._abs(path)
        result = await self._sh(_STAT, target)
        if result.exit_code == _NOT_FOUND:
            return None
        if result.exit_code != 0:
            err = result.stderr.decode("utf-8", errors="replace")
            if "Not a directory" in err or "No such file" in err:
                return None
            raise self._fail(result, target)
        first, _, link = result.stdout.decode("utf-8", errors="replace").strip() \
            .rpartition("\n")
        size, mtime, kind = first.split(" ", 2)
        return FileStat(size=int(size), mtime=float(mtime), is_dir=kind == "directory",
                        is_symlink=link.strip() == "1")

    async def stat_many(self, paths: Sequence[str]) -> list[FileStat | None]:
        """``noeta.core.execenv.stat_many``: one ``docker exec`` per
        :data:`_STAT_MANY_ARG_BYTES` of paths, not one per path."""
        targets = [self._abs(p) for p in paths]
        found: dict[str, FileStat] = {}
        start = 0
        while start < len(targets):
            end, size = start, 0
            while end < len(targets) and (end == start or size < _STAT_MANY_ARG_BYTES):
                size += len(targets[end].encode("utf-8", errors="surrogateescape")) + 1
                end += 1
            result = await self._sh(_STAT_MANY, *targets[start:end])
            fields = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
            for name, facts in zip(fields[0::2], fields[1::2], strict=False):
                size_text, _, rest = facts.partition(" ")
                mtime, _, kind = rest.partition(" ")
                try:
                    found[name] = FileStat(size=int(size_text), mtime=float(mtime),
                                           is_dir=kind == "directory")
                except ValueError:
                    continue
            start = end
        return [found.get(t) for t in targets]

    async def realpath(self, path: str) -> str:
        target = self._abs(path)
        result = await self._sh('realpath -m -- "$1"', target)
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")



def open_env(cwd: str, *, container: str, **kw: Any) -> ContainerExecEnv:
    """The ``container`` entry point of ``noeta.execenvs``. A ``sandbox``
    argument is accepted and left to provisioning (:func:`start_container`,
    :func:`ensure_container_proxy`): a container is confined as a whole."""
    kw.pop("sandbox", None)
    return ContainerExecEnv(container, cwd=cwd, **kw)
