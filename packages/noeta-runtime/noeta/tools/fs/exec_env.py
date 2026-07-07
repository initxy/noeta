"""``ExecEnv`` — the execution-backend seam under the fs / shell tool pack.

Every fs / shell tool performs two kinds of side effect against the
workspace: **file IO** (read / write / stat / directory walk) and
**process execution** (spawn a command). Today both go straight to the
local host — ``Path.read_bytes`` / ``Path.glob`` / ``subprocess`` via
``run_argv``. ``ExecEnv`` is the one seam those leaf operations route
through, so the *same* tool code can run either against the local host
(:class:`LocalExecEnv`, the default — byte-identical to the pre-seam
behaviour) or against a remote sandbox container (a later
``AioSandboxExecEnv`` that satisfies this same Protocol over HTTP).

Deliberately **IO-only**. Path *resolution* (the ``WorkspaceRoot``
containment fence — ``resolve`` / ``resolve_readable`` / ``relative`` /
``root``) stays on ``WorkspaceRoot`` and is unchanged: a tool still
resolves a user-supplied path to an absolute :class:`~pathlib.Path`
through the fence, then hands that *resolved* path to the ``ExecEnv`` for
the actual read / write / walk. For a remote backend the ``WorkspaceRoot``
is simply rooted at the container's workspace path (lexical containment —
absolute / ``..`` escapes still rejected; the container itself is the
real isolation boundary), and the ``ExecEnv`` IO methods are what cross
the process boundary.

:class:`LocalExecEnv` is **stateless**: it operates on the absolute
``Path`` the tool already resolved, so a tool passing ``resolved`` to
``exec_env.read_bytes(resolved)`` produces the exact same bytes as the
old ``resolved.read_bytes()``. That byte-identity is the contract this
seam is introduced under (Noeta's stable-prefix / resume moat): swapping
the *executor* must never perturb a tool's recorded output.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from noeta.tools.fs._subprocess import _RunOutcome, run_argv as _local_run_argv


__all__ = [
    "ExecEnv",
    "LocalExecEnv",
]

#: The injectable subprocess runner ``shell_run`` threads through to
#: ``run_argv`` (tests pass a fake to avoid shelling out on the happy path).
#: A ``subprocess.run``-shaped callable; ``None`` ⇒ the default local runner.
_SubprocRunner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@runtime_checkable
class ExecEnv(Protocol):
    """The file-IO + process-execution backend a fs / shell tool acts through.

    Every method operates on an **already-resolved absolute path** (the tool
    passes the ``Path`` it obtained from ``WorkspaceRoot.resolve`` /
    ``resolve_readable``); the ``ExecEnv`` never does containment itself. A
    ``LocalExecEnv`` reads the host filesystem; a sandbox backend fulfils the
    same shape against a container over its API.
    """

    # -- file reads --------------------------------------------------------
    def read_bytes(self, path: Path) -> bytes: ...

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str: ...

    # -- file writes -------------------------------------------------------
    def write_bytes(self, path: Path, body: bytes) -> None: ...

    def unlink(self, path: Path) -> None: ...

    # -- stat --------------------------------------------------------------
    def exists(self, path: Path) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def is_dir(self, path: Path) -> bool: ...

    def is_symlink(self, path: Path) -> bool: ...

    # -- directory walk ----------------------------------------------------
    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        """``base.glob(pattern)`` — one directory level of pattern expansion."""
        ...

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        """``base.rglob(pattern)`` — recursive pattern expansion."""
        ...

    # -- process -----------------------------------------------------------
    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        """Spawn ``argv`` under ``cwd``, capture output, enforce timeout + cap.

        Returns the same :class:`~noeta.tools.fs._subprocess._RunOutcome` the
        tools already consume.
        """
        ...


class LocalExecEnv:
    """The default :class:`ExecEnv`: the local host filesystem + subprocess.

    Stateless — every method is the exact ``Path`` / ``os`` / ``run_argv``
    operation the tools performed inline before the seam existed, so a tool
    routed through ``LocalExecEnv`` records byte-identical output. One shared
    instance is safe to reuse across every tool and task.
    """

    __slots__ = ()

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_bytes(self, path: Path, body: bytes) -> None:
        path.write_bytes(body)

    def unlink(self, path: Path) -> None:
        path.unlink()

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.glob(pattern)

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.rglob(pattern)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        return _local_run_argv(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            output_cap=output_cap,
            runner=runner,
        )
