"""Where tools touch files and run commands. ``LocalExecEnv`` is the host
machine; a container backend implements the same interface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from noeta.types import Struct


class RunResult(Struct):
    exit_code: int
    stdout: bytes
    stderr: bytes = b""
    timed_out: bool = False


class FileStat(Struct):
    size: int
    mtime: float
    is_dir: bool = False
    is_symlink: bool = False


class ExecEnv(Protocol):
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
    ) -> RunResult:
        """Run a program and wait. On timeout or cancellation the whole process
        group is killed."""
        ...

    async def read_bytes(self, path: str) -> bytes: ...

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write atomically, creating parent directories."""
        ...

    async def stat(self, path: str) -> FileStat | None:
        """``None`` when the path does not exist."""
        ...
