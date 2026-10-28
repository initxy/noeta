"""Where tools touch files and run commands. ``LocalExecEnv`` is the host
machine; a container backend implements the same interface."""

from __future__ import annotations

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
    ``noeta.toolkit.sandbox.sandbox_of``).
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

        ``env`` is laid over the backend's own (scrubbed) environment, never
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
