"""``WorkspaceRoot`` — the path-containment fence for the fs write tools.

Every write tool resolves user-supplied paths through one ``WorkspaceRoot``:
root and target are both canonicalised (``realpath``, symlink-resolving) and
the target must land under the root — or under a directory the host has
explicitly authorized — which defeats absolute paths, ``..`` escapes and
outward symlinks in a single check made before any IO. Reads are
deliberately NOT fenced (:func:`resolve_anywhere`): observation is not a
destructive act, so the wall stands only where the irreversible act is.
This is path resolution, not a sandbox — a tool that spawns an external
process can still touch the rest of the filesystem.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from noeta.protocols.tool import ToolResult


__all__ = [
    "WorkspaceEscape",
    "WorkspaceRoot",
    "WriteRootsResolver",
    "authorized_workspace",
    "path_within",
    "resolve_anywhere",
    "resolve_or_error",
    "tool_error",
]


def path_within(resolved: Path, root: Path) -> bool:
    """Whether ``resolved`` is ``root`` or lives under it.

    The one containment predicate every caller shares. Matching is
    **component-wise**, never string-prefix: ``/srv/app-old`` is NOT under
    ``/srv/app``, though its string form starts with it. Both arguments must
    already be canonicalised by the caller.
    """
    return resolved == root or resolved.is_relative_to(root)


class WorkspaceEscape(ValueError):
    """Raised when a user-supplied path resolves outside the workspace."""


@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    """Symlink-safe path containment seam.

    ``root`` is canonicalised (``realpath``) at construction; ``display``
    keeps the original user-facing form for messages.
    """

    root: Path
    display: str
    #: Directories OUTSIDE ``root`` this instance may also resolve into — the
    #: host's authorization surface for out-of-workspace writes. Must already
    #: be canonicalised by the caller, exactly like ``root``. Empty ⇒ the
    #: single-root wall.
    extra_roots: tuple[Path, ...] = ()
    #: When ``True``, ``resolve`` normalises *lexically* instead of resolving
    #: symlinks — the fence for a **sandbox** workspace, whose ``root`` is a
    #: container path that does not exist on the host, making a host
    #: ``realpath`` both wrong and impossible. The container is the real
    #: isolation boundary there; this degrades to a tidiness fence that still
    #: rejects ``..`` above root and absolute escapes.
    lexical: bool = False

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "WorkspaceRoot":
        """Build a root from a user-supplied directory path.

        The directory must already exist: this is a workspace to operate
        inside, not a path to be created on the fly.
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
        """Build a *lexical* root at a **container** working directory.

        The directory lives inside a sandbox container, not on the host, so it
        is neither ``realpath``-resolved nor checked for existence here; the
        remote ``ExecEnv`` performs the actual IO. It must be absolute.
        """
        original = os.fspath(container_dir)
        root = Path(os.path.normpath(original))
        if not root.is_absolute():
            raise WorkspaceEscape(
                f"container workspace path {original!r} must be absolute"
            )
        return cls(root=root, display=original, lexical=True)

    def with_extra_roots(self, roots: Sequence[Path]) -> "WorkspaceRoot":
        """Return a copy that also resolves into ``roots``.

        The per-call widening point: the write tools rebuild their fence from
        the authorized directories at invoke time, so a grant made *while* a
        task is paused takes effect on the resumed call without rebuilding the
        tool set — which would move the stable prefix.
        """
        return replace(self, extra_roots=tuple(roots))

    def canonicalise(self, target: str) -> Path:
        """Canonicalise ``target`` the way :meth:`resolve` does, without the
        containment check — realpath for a host root, normpath for a lexical
        (container) root, relative targets joined onto the workspace."""
        joined = self.root / target if not os.path.isabs(target) else Path(target)
        if self.lexical:
            # Collapse ``..`` / ``.`` without touching the host FS: there is no
            # host symlink to follow for a container path, and the container
            # itself is the isolation boundary.
            return Path(os.path.normpath(os.fspath(joined)))
        return Path(os.path.realpath(os.fspath(joined)))

    def allows(self, resolved: Path) -> bool:
        """Whether an already-canonicalised path is inside the fence — the
        workspace itself or any authorized ``extra_roots`` entry."""
        return path_within(resolved, self.root) or any(
            path_within(resolved, extra) for extra in self.extra_roots
        )

    def resolve(self, target: str) -> Path:
        """Return ``target`` joined under the workspace, canonicalised.

        ``target`` may be relative or absolute; either way the result must
        live under ``self.root`` or an authorized ``extra_roots`` entry
        **after** resolution, and raises ``WorkspaceEscape`` otherwise.
        """
        if not isinstance(target, str) or not target:
            raise WorkspaceEscape("path must be a non-empty string")
        resolved = self.canonicalise(target)
        if not self.allows(resolved):
            raise WorkspaceEscape(
                f"path {target!r} resolves outside workspace {self.display!r}"
            )
        return resolved

    def relative(self, resolved: Path) -> str:
        """Return ``resolved`` as a workspace-relative POSIX string.

        POSIX form keeps tool ``output`` / ``summary`` display strings
        deterministic across operating systems. A path outside the workspace
        has no relative form and is shown absolute, which is also the honest
        display: it tells the reader the path left the workspace.
        """
        if not path_within(resolved, self.root):
            return resolved.as_posix()
        rel = resolved.relative_to(self.root) if resolved != self.root else Path(".")
        return rel.as_posix()


#: ``task_id -> the directories this task may write outside its workspace``.
#: Resolved per call rather than bound at build time, so an authorization
#: granted while the task sits paused on an approval takes effect on the
#: resumed call and widening it never perturbs the tool set / stable prefix.
WriteRootsResolver = Callable[[str], Sequence[str]]


def authorized_workspace(
    workspace: "WorkspaceRoot",
    resolver: "Optional[WriteRootsResolver]",
    ctx: object,
) -> "WorkspaceRoot":
    """The write fence for one call: ``workspace`` widened by whatever the
    host authorizes for the task behind ``ctx``.

    Fails **closed** in every degenerate case — no resolver, no task id on the
    context, a resolver that raises, a non-absolute entry — by returning the
    unwidened workspace, so a broken authorization path can only ever refuse a
    write, never permit one. Entries are canonicalised the same way the root
    is, keeping containment symlink-safe and component-wise.
    """
    if resolver is None:
        return workspace
    metadata = getattr(ctx, "metadata", None) or {}
    task_id = metadata.get("task_id") if isinstance(metadata, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return workspace
    try:
        raw = resolver(task_id)
    except Exception:  # noqa: BLE001 - an authorization fault denies, never grants
        return workspace
    roots: list[Path] = []
    for entry in raw or ():
        if not isinstance(entry, str) or not entry or not os.path.isabs(entry):
            continue
        roots.append(
            Path(os.path.normpath(entry))
            if workspace.lexical
            else Path(os.path.realpath(entry))
        )
    return workspace.with_extra_roots(roots) if roots else workspace


def tool_error(tool_name: str, message: str) -> ToolResult:
    """Uniform ``ToolResult(success=False, summary="<tool>: <message>")``."""
    return ToolResult(success=False, summary=f"{tool_name}: {message}")


def resolve_or_error(
    workspace: "WorkspaceRoot", tool_name: str, path: str
) -> "Path | ToolResult":
    """Resolve ``path`` under ``workspace`` or return a failure ``ToolResult``.

    A ``WorkspaceEscape`` is degraded to a failed result so a malformed or
    escaping path is answered to the model instead of crashing the worker.
    """
    try:
        return workspace.resolve(path)
    except WorkspaceEscape as exc:
        return tool_error(tool_name, str(exc))


def resolve_anywhere(
    workspace: "WorkspaceRoot", tool_name: str, path: str
) -> "Path | ToolResult":
    """Resolve ``path`` for *reading* — unfenced.

    A relative path is joined onto the workspace; an absolute path is
    canonicalised and returned as-is, so a neighbouring checkout, a skill
    pack's bundled reference and a file under ``/usr/share`` are all reachable
    by naming them. A read is observation, not mutation, and the fence that
    matters guards the irreversible acts (:func:`resolve_or_error`). Anything
    the host must not disclose has to be kept off the host or out of the
    process — a path check inside the tool is not that boundary, since
    ``shell_run`` reads the same bytes.

    Only the malformed-argument case fails, so the return shape stays
    ``Path | ToolResult`` like its fenced sibling.
    """
    if not isinstance(path, str) or not path:
        return tool_error(tool_name, "path must be a non-empty string")
    return workspace.canonicalise(path)


class FsWriteMode(str, Enum):
    """Pre-run write policy passed to the edit tools at construction.

    ``DRY_RUN`` produces the proposed diff artifact + ``applied=False``;
    ``APPLY`` performs the write. The Engine never sees this enum — the
    decision is bound into the tools themselves.
    """

    DRY_RUN = "dry_run"
    APPLY = "apply"
