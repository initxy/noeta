"""``ExecEnv`` — the file-IO and process-execution backend under the fs tools.

Routing both kinds of side effect through one seam lets identical tool code
run against the local host (:class:`LocalExecEnv`) or against a remote sandbox
container (an adapter in the ``sandbox`` built-in plugin). The seam is
deliberately IO-only: path *resolution* stays on ``WorkspaceRoot``, so a tool
pushes a user-supplied path through the containment fence first and hands down
only the resolved absolute path — a remote backend roots that fence at the
container's workspace path and leans on the container itself as the real
isolation boundary. Swapping the executor must never perturb a tool's recorded
output; that byte-identity is what keeps a resume reproducible.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from noeta.runtime.subproc import (
    RunOutcome,
    run_argv as _local_run_argv,
)


__all__ = [
    "ExclusiveCreateError",
    "ExclusiveCreateExists",
    "ExclusiveCreateFailed",
    "ExclusiveCreateWriteFailed",
    "ExecEnv",
    "LocalExecEnv",
    "TreeSnapshot",
]


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte to ``fd`` — ``os.write`` may short-write.

    A zero-length write or an ``OSError`` is a failure; a partial write is
    NEVER reported as success.
    """
    mv = memoryview(data)
    total = 0
    while total < len(data):
        n = os.write(fd, mv[total:])
        if n <= 0:
            raise OSError("short write (os.write returned 0)")
        total += n


class ExclusiveCreateError(OSError):
    """A :meth:`ExecEnv.create_exclusive` failure, carrying the rollback verb.

    A caller must know whether the target exists after a failed atomic create
    before it can clean up, so each failure mode fixes ``recover`` rather than
    leaving the caller to guess from ``errno``. Subclasses ``OSError`` so a
    caller's ``except OSError`` rollback path still catches it.
    """

    #: The rollback verb ``apply_patch._fail`` acts on ("none" | "delete").
    recover: str = "none"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExclusiveCreateExists(ExclusiveCreateError):
    """The exclusive ``O_EXCL`` open failed because the path already exists —
    the target was NOT created by this call, so ``recover="none"``."""

    recover = "none"


class ExclusiveCreateFailed(ExclusiveCreateError):
    """The exclusive open failed for a non-existence reason (permissions,
    missing parent, …) — nothing created, so ``recover="none"``."""

    recover = "none"


class ExclusiveCreateWriteFailed(ExclusiveCreateError):
    """The open SUCCEEDED but the subsequent write / close failed — the file
    now EXISTS and must be deleted, so ``recover="delete"``."""

    recover = "delete"

#: A ``subprocess.run``-shaped callable ``shell_run`` threads through to
#: ``run_argv``; ``None`` ⇒ the default local runner. Injectable so tests
#: need not shell out on the happy path.
SubprocRunner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class TreeSnapshot:
    """One recursive listing of file trees, with selected contents inlined.

    * ``files`` — every **regular file** under any of the requested roots
      (recursive, symlinks followed), sorted and de-duplicated. Directories are
      never listed, so a consumer needs no per-entry ``is_file`` probe.
    * ``contents`` — the raw bytes of each listed file whose *name* equals the
      requested ``content_name``, keyed by its path. A file that could not be
      read is listed in ``files`` but absent here.
    """

    files: tuple[Path, ...]
    contents: dict[Path, bytes]


@runtime_checkable
class ExecEnv(Protocol):
    """The file-IO + process-execution backend a fs / shell tool acts through.

    Every method takes an **already-resolved absolute path**: containment is
    the caller's ``WorkspaceRoot``'s job, and an implementation must not
    re-interpret or re-root what it is handed.
    """

    # -- file reads --------------------------------------------------------
    def read_bytes(self, path: Path) -> bytes: ...

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str: ...

    # -- file writes -------------------------------------------------------
    def write_bytes(self, path: Path, body: bytes) -> None: ...

    def create_exclusive(self, path: Path, body: bytes) -> None:
        """Atomically create ``path`` and write ``body`` — never overwrite.

        The create must fail if the path already exists. On failure raises an
        :class:`ExclusiveCreateError` subclass whose ``recover`` / ``reason``
        tell the caller how to roll back.
        """
        ...

    def unlink(self, path: Path) -> None: ...

    def mkdir(self, path: Path) -> None:
        """Create ``path`` and any missing parents; an existing directory is
        not an error (``parents=True, exist_ok=True``). A rewind restore leans
        on both: the directory a restored file lived in may itself have been
        removed inside the rewound span."""
        ...

    # -- stat --------------------------------------------------------------
    def exists(self, path: Path) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def is_dir(self, path: Path) -> bool: ...

    def is_symlink(self, path: Path) -> bool: ...

    def mtime(self, path: Path) -> float:
        """Modification time (seconds since epoch), ``0.0`` when the backend
        cannot stat it. ``Glob`` sorts newest-first on this; a backend without
        cheap stat may return a constant and degrade to the alphabetical
        tiebreak."""
        ...

    # -- directory walk ----------------------------------------------------
    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        """Match :meth:`pathlib.Path.glob` semantics exactly."""
        ...

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        """Match :meth:`pathlib.Path.rglob` semantics exactly."""
        ...

    def tree_snapshot(
        self, roots: Sequence[Path], *, content_name: str
    ) -> TreeSnapshot:
        """Batch walk: list every regular file under ``roots`` and inline the
        bytes of each file named ``content_name`` — in ONE backend operation.

        Bulk discovery (skill indexing) done with ``rglob`` + ``is_file`` +
        ``read_text`` costs O(N) round-trips on a remote backend, where the
        per-call fixed cost dominates; this primitive costs O(1). A missing
        root contributes nothing (not an error).
        """
        ...

    # -- process -----------------------------------------------------------
    @property
    def supports_background(self) -> bool:
        """Whether ``shell_run(run_in_background=True)`` is valid on this backend.

        ``ProcessRegistry`` spawns detached HOST subprocesses and cannot reach
        into a container, so a container backend must return ``False`` and let
        ``shell_run`` refuse the launch rather than silently run the job on the
        wrong side of the isolation boundary."""
        ...

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[SubprocRunner] = None,
    ) -> RunOutcome:
        """Spawn ``argv`` under ``cwd``, capture output, enforce timeout + cap.

        The timeout and the output cap are the implementation's obligation,
        not the caller's: a backend that ignores them lets one command hang or
        flood the recorded stream.
        """
        ...


class LocalExecEnv:
    """The default :class:`ExecEnv`: the local host filesystem + subprocess.

    Stateless, so one shared instance is safe to reuse across every tool and
    task.
    """

    __slots__ = ()

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return path.read_text(encoding=encoding)

    def write_bytes(self, path: Path, body: bytes) -> None:
        path.write_bytes(body)

    def create_exclusive(self, path: Path, body: bytes) -> None:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise ExclusiveCreateExists(
                "path created by another process (exclusive create)"
            ) from exc
        except OSError as exc:
            raise ExclusiveCreateFailed(f"create failed: {exc}") from exc
        # The file now EXISTS — any failure (write OR close) must delete it;
        # a close OSError must NOT escape and bypass rollback (close can report
        # a deferred write-back error).
        try:
            _write_all(fd, body)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise ExclusiveCreateWriteFailed(f"write failed: {exc}") from exc
        try:
            os.close(fd)
        except OSError as exc:
            raise ExclusiveCreateWriteFailed(f"close failed: {exc}") from exc

    def unlink(self, path: Path) -> None:
        path.unlink()

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    @property
    def supports_background(self) -> bool:
        return True

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def glob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.glob(pattern)

    def rglob(self, base: Path, pattern: str) -> Iterable[Path]:
        return base.rglob(pattern)

    def tree_snapshot(
        self, roots: Sequence[Path], *, content_name: str
    ) -> TreeSnapshot:
        # Symlinked directories are followed (matching the sandbox backend's
        # ``find -L``), so a realpath cycle guard is mandatory or a symlink
        # loop inside the workspace walks forever.
        files: set[Path] = set()
        contents: dict[Path, bytes] = {}
        seen_dirs: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
                try:
                    real = os.path.realpath(dirpath)
                except OSError:
                    dirnames[:] = []
                    continue
                if real in seen_dirs:
                    dirnames[:] = []
                    continue
                seen_dirs.add(real)
                for filename in filenames:
                    entry = Path(dirpath) / filename
                    try:
                        if not entry.is_file():
                            continue
                    except OSError:
                        continue
                    files.add(entry)
                    if entry.name == content_name and entry not in contents:
                        try:
                            contents[entry] = entry.read_bytes()
                        except OSError:
                            continue
        return TreeSnapshot(tuple(sorted(files)), contents)

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: int,
        output_cap: int,
        runner: Optional[SubprocRunner] = None,
    ) -> RunOutcome:
        return _local_run_argv(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            output_cap=output_cap,
            runner=runner,
        )
