"""Where tools touch files and run commands. ``LocalExecEnv`` is the host
machine; a container backend implements the same interface."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
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


def is_local(env: object) -> bool:
    """Whether ``env`` runs commands on this machine (so facts about this
    machine, such as its shell and OS, describe it)."""
    return bool(getattr(env, "local", False))
