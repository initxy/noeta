"""``ExecEnv`` — the execution-backend seam under the fs / shell tool pack.

Every fs / shell tool performs two kinds of side effect against the
workspace: **file IO** (read / write / stat / directory walk) and
**process execution** (spawn a command). Today both go straight to the
local host — ``Path.read_bytes`` / ``Path.glob`` / ``subprocess`` via
``run_argv``. ``ExecEnv`` is the one seam those leaf operations route
through, so the *same* tool code can run either against the local host
(:class:`LocalExecEnv`, the default — byte-identical to the pre-seam
behaviour) or against a remote sandbox container (an adapter satisfying this
same Protocol over HTTP — the AIO one lives in the ``sandbox`` built-in
plugin, ``noeta.builtins.sandbox.impl.exec_env``, since microkernel M2).

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

import contextlib
import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from noeta.runtime.subproc import (
    _RunOutcome,
    cap_stream,
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
    """Write every byte to ``fd`` (``os.write`` may short-write).

    A zero-length write or an ``OSError`` is a failure — a partial write is
    NEVER treated as success. Moved here from ``patch.py`` when
    ``apply_patch``'s exclusive-create routed through
    :meth:`ExecEnv.create_exclusive`; the ``os.write`` seam the patch tests
    monkeypatch is unchanged.
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

    ``apply_patch`` distinguishes three outcomes of an atomic create so it can
    pick the right recovery (``recover="none"`` when the target was never
    created by us, ``recover="delete"`` when it was created then the write /
    close failed). Subclasses fix ``recover``; ``reason`` is the exact
    human-facing message the tool surfaces (preserved byte-for-byte from the
    pre-seam inline dance). Subclasses ``OSError`` so the tool's existing
    ``except OSError`` rollback sites keep working.
    """

    #: The rollback verb ``apply_patch._fail`` acts on ("none" | "delete").
    recover: str = "none"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        #: The exact failure message the tool surfaces as its ``reason``.
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

#: The injectable subprocess runner ``shell_run`` threads through to
#: ``run_argv`` (tests pass a fake to avoid shelling out on the happy path).
#: A ``subprocess.run``-shaped callable; ``None`` ⇒ the default local runner.
_SubprocRunner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class TreeSnapshot:
    """One recursive listing of file trees, with selected contents inlined.

    Returned by :meth:`ExecEnv.tree_snapshot` — the batch primitive that
    replaces a per-file ``rglob`` / ``is_file`` / ``read_text`` walk when the
    backend has a per-call fixed cost (a sandbox HTTP round-trip, issue 46).

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

    def create_exclusive(self, path: Path, body: bytes) -> None:
        """Atomically create ``path`` and write ``body`` — never overwrite.

        The create must fail if the path already exists. On failure raises a
        :class:`ExclusiveCreateError` subclass whose ``recover`` /``reason``
        tell ``apply_patch`` how to roll back (the seam preserves the exact
        recovery semantics ``apply_patch`` had inline before the seam existed).
        """
        ...

    def unlink(self, path: Path) -> None: ...

    def mkdir(self, path: Path) -> None:
        """Create ``path`` and any missing parents (``parents=True,
        exist_ok=True``). Used by the rewind restore (T7) to re-create a
        directory an edited-then-restored file lived in when the rewound span
        had removed it. An existing directory is not an error."""
        ...

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

    def tree_snapshot(
        self, roots: Sequence[Path], *, content_name: str
    ) -> TreeSnapshot:
        """Batch walk: list every regular file under ``roots`` and inline the
        bytes of each file named ``content_name`` — in ONE backend operation.

        The batch counterpart of ``rglob`` + ``is_file`` + ``read_text`` for
        bulk discovery (skill indexing, issue 46): on a remote backend each of
        those is a full round-trip with a fixed cost that dominates, so a
        per-file walk over N files costs O(N) round-trips; this primitive
        costs O(1). A missing root contributes nothing (not an error).
        """
        ...

    # -- process -----------------------------------------------------------
    @property
    def supports_background(self) -> bool:
        """Whether ``shell_run(run_in_background=True)`` is valid on this backend.

        The host background runner (``ProcessRegistry``) spawns detached HOST
        subprocesses — it cannot reach into a container, and AIO exposes no
        durable job handle (a v2 concern). So a container backend returns
        ``False`` and ``shell_run`` refuses a background launch cleanly; the
        local host returns ``True``."""
        ...

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

        Returns the same :class:`~noeta.runtime.subproc._RunOutcome` the
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

    def create_exclusive(self, path: Path, body: bytes) -> None:
        # The exact fd-level dance apply_patch ran inline before the seam
        # existed: exclusive O_EXCL open, write-all, close — each failure
        # mapped to the recovery verb the tool expects. The ``os.open`` /
        # ``os.write`` / ``os.close`` seams the patch tests monkeypatch are
        # unchanged; only their home moved.
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
        # The host runner spawns host subprocesses — valid for the local host.
        return True

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

    def tree_snapshot(
        self, roots: Sequence[Path], *, content_name: str
    ) -> TreeSnapshot:
        # Local IO has no per-call fixed cost, so this is simply the walk the
        # per-file methods would do, folded into one result. Symlinked
        # directories are followed with a realpath cycle guard — the same
        # semantics as the sandbox backend's ``find -L`` (and the host skill
        # indexer's own walk, which does not route through this).
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
        runner: Optional[_SubprocRunner] = None,
    ) -> _RunOutcome:
        return _local_run_argv(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            output_cap=output_cap,
            runner=runner,
        )
