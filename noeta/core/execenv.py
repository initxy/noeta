"""Where tools touch files and run commands. ``LocalExecEnv`` is the host
machine; a container backend implements the same interface."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Protocol

from noeta.core.types import Struct


class RunResult(Struct):
    exit_code: int
    stdout: bytes
    stderr: bytes = b""
    timed_out: bool = False
    #: ``run(output_cap=N)`` kept only the last ``N`` bytes of the stream.
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class FileStat(Struct):
    size: int
    mtime: float
    is_dir: bool = False
    is_symlink: bool = False


class ExecEnv(Protocol):
    """Files and commands for tools.

    Optional members: ``local: bool`` (commands run on this machine; read
    by :func:`is_local`) and ``sandbox`` (a
    ``noeta.toolkit.sandbox.CommandSandbox`` commands may run inside; read by
    ``noeta.toolkit.sandbox.sandbox_of``) and ``temp_dir: str | None`` (a
    temporary directory of the env's own, for files a tool keeps for the task
    such as background job logs), ``stat_many`` (see :func:`stat_many`).
    """

    #: The working directory paths are resolved against, as the env sees it.
    cwd: str

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
        """Run a program and wait. On timeout or cancellation the whole process
        group is killed.

        ``env`` is laid over the backend's own environment, never
        a replacement for it. ``output_cap`` bounds each captured stream while
        it is read: only its last ``output_cap`` bytes are kept and the
        ``*_truncated`` flag says the head was dropped. A timed-out run
        returns ``timed_out=True`` with ``exit_code=-1`` and whatever output
        it produced. A missing program raises ``FileNotFoundError`` (or exits
        127 where a shell stands in between)."""
        ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes of ``path`` from byte ``offset``; fewer only
        at end of file (empty past it). Lets a large file be read in bounded
        chunks."""
        ...

    async def realpath(self, path: str) -> str:
        """``path`` made absolute (relative to ``cwd``) with symlinks and
        ``..`` resolved, as the env sees it. A missing tail is kept as named,
        like ``os.path.realpath``."""
        ...

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write atomically, creating parent directories."""
        ...

    async def stat(self, path: str) -> FileStat | None:
        """``None`` when the path does not exist."""
        ...


#: Paths ``stat_many`` stats at once on an env without its own ``stat_many``.
_STAT_CONCURRENCY = 32


async def stat_many(env: ExecEnv, paths: Sequence[str]) -> list[FileStat | None]:
    """Size, modification time and kind of what each of ``paths`` names,
    following symlinks, in order: ``None`` where nothing is there (a missing
    path, a symlink to nothing) or it cannot be examined; ``is_symlink`` is
    not reported. The env's own ``stat_many(paths)`` answers when it has one
    (a remote env checks many paths in one round trip); otherwise each path
    goes to ``env.stat``, a few at a time."""
    own = getattr(env, "stat_many", None)
    if own is not None:
        return list(await own(paths))
    gate = asyncio.Semaphore(_STAT_CONCURRENCY)

    async def one(path: str) -> FileStat | None:
        async with gate:
            try:
                return await env.stat(path)
            except OSError:
                return None

    return list(await asyncio.gather(*(one(p) for p in paths)))
