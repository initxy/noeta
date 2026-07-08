"""`WorkspaceRoot` — the path-containment seam for the fs tool pack.

Every fs tool resolves user-supplied paths through one ``WorkspaceRoot``
instance. The resolver canonicalises both the workspace root and the
target through ``os.path.realpath`` (symlink-resolving) and asserts the
target lives under the realpath root. This defeats three classes of
escape:

* absolute paths (``/etc/passwd``) — ``realpath`` of an absolute path
  is itself; the containment check fails.
* ``..``-rooted relatives (``../outside``) — ``realpath`` collapses
  them; the containment check fails.
* symlinks pointing outside the workspace — ``realpath`` resolves them
  to the target; the containment check fails.

The check is done at *path-resolution* time, before any IO. Containment
is the only seam — once a path is inside, fs tools are free to read /
list / write it (subject to the dry-run-by-default write policy that I2
adds). Per PRD §B19, this is not a sandbox: a tool that invokes
external processes (``shell_run``, I5) can still touch the rest of the
filesystem. ``WorkspaceRoot`` is about *path resolution*, not subprocess
containment.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from noeta.protocols.tool import ToolResult


__all__ = [
    "WorkspaceEscape",
    "WorkspaceRoot",
    "resolve_or_error",
    "resolve_readable",
    "tool_error",
]


class WorkspaceEscape(ValueError):
    """Raised when a user-supplied path resolves outside the workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    """Symlink-safe path containment seam.

    ``root`` is the workspace directory the coding agent operates inside.
    It is canonicalised (``realpath``) at construction; the original
    user-facing form is kept for messages via ``display``.
    """

    root: Path
    display: str
    #: When ``True``, ``resolve`` normalises *lexically* (``os.path.normpath``)
    #: instead of ``os.path.realpath`` — the containment fence for a **sandbox**
    #: workspace, whose ``root`` is a *container* path that does not exist on the
    #: host (so a host ``realpath`` / symlink-resolve is both wrong and
    #: impossible). The container itself is the real isolation boundary (D7);
    #: this stays a tidiness fence (reject ``..`` above root / absolute
    #: escapes). Default ``False`` ⇒ today's host realpath behaviour, byte-equal
    #: for every existing construction.
    lexical: bool = False

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "WorkspaceRoot":
        """Build a root from a user-supplied directory path.

        The directory must exist and be a directory; this is a coding
        agent's workspace, not a path to be created on the fly.
        """
        original = os.fspath(path)
        real = Path(os.path.realpath(original))
        if not real.is_dir():
            raise WorkspaceEscape(
                f"workspace path {original!r} is not an existing directory"
            )
        return cls(root=real, display=original)

    @classmethod
    def for_container(cls, container_dir: str | os.PathLike[str]) -> "WorkspaceRoot":
        """Build a *lexical* root at a **container** working directory (D7).

        The directory lives inside a sandbox container, not on the host, so it
        is neither ``realpath``-resolved nor checked for existence here —
        ``resolve`` does purely lexical (``normpath``) containment and the
        remote ``ExecEnv`` performs the actual IO. The path must be absolute
        (a container work dir like ``/home/gem/workspace``).
        """
        original = os.fspath(container_dir)
        root = Path(os.path.normpath(original))
        if not root.is_absolute():
            raise WorkspaceEscape(
                f"container workspace path {original!r} must be absolute"
            )
        return cls(root=root, display=original, lexical=True)

    def resolve(self, target: str) -> Path:
        """Return ``target`` joined under the workspace, canonicalised.

        ``target`` may be relative (joined to the root) or absolute; either
        way the result must live under ``self.root`` after resolution
        (``realpath`` for a host root, ``normpath`` for a lexical / container
        root). Raises ``WorkspaceEscape`` otherwise.
        """
        if not isinstance(target, str) or not target:
            raise WorkspaceEscape("path must be a non-empty string")
        joined = self.root / target if not os.path.isabs(target) else Path(target)
        if self.lexical:
            # Lexical containment for a container root: collapse ``..`` / ``.``
            # without touching the host FS (no symlink resolution — there is no
            # host symlink to follow; the container is the isolation boundary).
            resolved = Path(os.path.normpath(os.fspath(joined)))
        else:
            resolved = Path(os.path.realpath(os.fspath(joined)))
        # ``Path.is_relative_to`` is the Python-3.9+ structural form;
        # equivalent to ``str(resolved).startswith(str(root)+sep)`` plus
        # the equality case.
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceEscape(
                f"path {target!r} resolves outside workspace {self.display!r}"
            )
        return resolved

    def relative(self, resolved: Path) -> str:
        """Return ``resolved`` as a workspace-relative POSIX string.

        Used for stable display strings in tool ``output`` / ``summary``
        (cross-OS deterministic).
        """
        rel = resolved.relative_to(self.root) if resolved != self.root else Path(".")
        return rel.as_posix()


def tool_error(tool_name: str, message: str) -> ToolResult:
    """Uniform ``ToolResult(success=False, summary="<tool>: <message>")``.

    The single failure shape used by the read- and write-side fs tools
    (``read`` / ``glob`` / ``grep`` / ``edit``
    / ``write``). ``shell``'s ``_err`` and ``apply_patch`` /
    ``run_skill_script``'s one-arg ``_err`` are *not* folded in here: they
    keep their own form (the latter two bind the tool name as a constant),
    so this seam only unifies the byte-identical two-arg variant.
    """
    return ToolResult(success=False, summary=f"{tool_name}: {message}")


def resolve_or_error(
    workspace: "WorkspaceRoot", tool_name: str, path: str
) -> "Path | ToolResult":
    """Resolve ``path`` under ``workspace`` or return a failure ``ToolResult``.

    Wraps ``WorkspaceRoot.resolve``: a ``WorkspaceEscape`` is degraded to
    ``tool_error(tool_name, str(exc))`` so a malformed / escaping path does
    not crash the worker. This is the shared form of the per-tool
    ``_resolve`` that ``read.py`` and ``edit.py`` each used to define.
    """
    try:
        return workspace.resolve(path)
    except WorkspaceEscape as exc:
        return tool_error(tool_name, str(exc))


def resolve_readable(
    workspace: "WorkspaceRoot",
    extra_roots: Sequence[Path],
    tool_name: str,
    path: str,
) -> "Path | ToolResult":
    """Resolve ``path`` for *reading* — under the workspace, or failing that
    under one of ``extra_roots``.

    ``extra_roots`` are read-only allowlisted directories that live
    **outside** the workspace — the skill packs in ``~/.noeta/skills`` /
    the built-in dir, whose absolute ``Base directory for this skill:``
    line the renderer hands the model so it can read a skill's bundled
    references with the ordinary ``read`` tool. The widening applies to
    reads only (the write-side tools keep the single-root wall) and only
    to **absolute** targets: a relative path always resolves against the
    workspace, exactly as before.

    Containment stays realpath-based, so a symlink under a skill root that
    escapes every allowed root still fails. ``extra_roots`` must already be
    canonicalised (``Path.resolve()``) by the caller. With an empty
    ``extra_roots`` this degrades identically to :func:`resolve_or_error`.
    """
    try:
        return workspace.resolve(path)
    except WorkspaceEscape as exc:
        if isinstance(path, str) and path and os.path.isabs(path):
            resolved = Path(os.path.realpath(path))
            for root in extra_roots:
                if resolved == root or resolved.is_relative_to(root):
                    return resolved
        return tool_error(tool_name, str(exc))
