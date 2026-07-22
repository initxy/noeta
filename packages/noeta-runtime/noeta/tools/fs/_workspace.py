"""`WorkspaceRoot` — the path-containment seam for the fs tool pack.

Every *write* fs tool resolves user-supplied paths through one
``WorkspaceRoot`` instance. The resolver canonicalises both the workspace
root and the target through ``os.path.realpath`` (symlink-resolving) and
asserts the target lives under the realpath root (or under one of the
``extra_roots`` the host has authorized). This defeats three classes of
escape:

* absolute paths (``/etc/passwd``) — ``realpath`` of an absolute path
  is itself; the containment check fails.
* ``..``-rooted relatives (``../outside``) — ``realpath`` collapses
  them; the containment check fails.
* symlinks pointing outside the workspace — ``realpath`` resolves them
  to the target; the containment check fails.

The check is done at *path-resolution* time, before any IO. Per PRD §B19,
this is not a sandbox: a tool that invokes external processes
(``shell_run``, I5) can still touch the rest of the filesystem.
``WorkspaceRoot`` is about *path resolution*, not subprocess containment.

**Reads are not fenced** (:func:`resolve_anywhere`). A relative path still
resolves under the workspace, but an absolute path is read wherever it
points: reading a neighbouring checkout is the everyday "look at how the
other repo does it" move, and a read is not a destructive act, so paying
for it with a hard failure the model cannot recover from bought nothing.
The asymmetry is deliberate — the wall stands where the irreversible act
is (write / edit / patch), not where the observation is.

**Writes widen by authorization, not by escape.** ``extra_roots`` carries
the directories a host has decided this caller may write to. The runtime
takes them as given and applies the same component-wise containment it
applies to the workspace itself; deciding *what* is in that tuple (asking
a human, remembering the answer) is the host's business — see the
noeta-agent product's approval gate, which pauses an out-of-workspace
write, records the owner's ruling as a durable grant, and feeds the
granted directories back in here on the resumed call.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
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

    The one containment predicate the fence, the display helper and the
    product's approval gate all share. Matching is **component-wise**
    (``Path.is_relative_to``), never string-prefix: ``/srv/app-old`` is NOT
    under ``/srv/app``, though its string form starts with it. Both
    arguments must already be canonicalised by the caller.
    """
    return resolved == root or resolved.is_relative_to(root)


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
    #: Directories OUTSIDE ``root`` this instance may also resolve into —
    #: the host's authorization surface for out-of-workspace writes (the
    #: product fills it from the owner's standing grants; the CLI / a bare
    #: SDK embedding leaves it empty and keeps the single-root wall). Must
    #: already be canonicalised (``realpath``) by the caller, exactly like
    #: ``root``. Empty (default) ⇒ byte-identical to a single-root build.
    extra_roots: tuple[Path, ...] = ()
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

    def with_extra_roots(self, roots: Sequence[Path]) -> "WorkspaceRoot":
        """Return a copy that also resolves into ``roots``.

        The host's per-call widening point: the write tools rebuild their
        fence from the authorized directories at invoke time, so a grant
        made *while* a task is paused takes effect on the resumed call
        without rebuilding the tool set (which would move the stable
        prefix). An empty ``roots`` returns an equivalent instance.
        """
        return replace(self, extra_roots=tuple(roots))

    def canonicalise(self, target: str) -> Path:
        """Canonicalise ``target`` the way :meth:`resolve` does, without the
        containment check — realpath for a host root, normpath for a lexical
        (container) root, relative targets joined onto the workspace."""
        joined = self.root / target if not os.path.isabs(target) else Path(target)
        if self.lexical:
            # Lexical containment for a container root: collapse ``..`` / ``.``
            # without touching the host FS (no symlink resolution — there is no
            # host symlink to follow; the container is the isolation boundary).
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

        ``target`` may be relative (joined to the root) or absolute; either
        way the result must live under ``self.root`` — or under an authorized
        ``extra_roots`` entry — after resolution (``realpath`` for a host
        root, ``normpath`` for a lexical / container root). Raises
        ``WorkspaceEscape`` otherwise.
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

        Used for stable display strings in tool ``output`` / ``summary``
        (cross-OS deterministic). A path outside the workspace — an unfenced
        read, or a write into an authorized ``extra_roots`` directory — has
        no relative form and is shown absolute, which is also the honest
        display: it tells the reader the path left the workspace.
        """
        if not path_within(resolved, self.root):
            return resolved.as_posix()
        rel = resolved.relative_to(self.root) if resolved != self.root else Path(".")
        return rel.as_posix()


#: ``task_id -> the directories this task may write outside its workspace``.
#: The host's authorization seam for the write tools, resolved per call (NOT
#: bound at build time) so an authorization granted while the task sits paused
#: on an approval takes effect on the resumed call — and so widening it never
#: perturbs the tool set / stable prefix. ``None`` ⇒ the single-root wall.
WriteRootsResolver = Callable[[str], Sequence[str]]


def authorized_workspace(
    workspace: "WorkspaceRoot",
    resolver: "Optional[WriteRootsResolver]",
    ctx: object,
) -> "WorkspaceRoot":
    """The write fence for one call: ``workspace`` widened by whatever the
    host authorizes for the task behind ``ctx``.

    Fails **closed** in every degenerate case — no resolver, no task id on the
    context, a resolver that raises, a non-directory entry — by returning the
    unwidened workspace, so a broken authorization path can only ever refuse a
    write, never permit one. Entries are canonicalised the same way the root
    is (``realpath``, or ``normpath`` under a lexical / container root), so
    containment stays symlink-safe and component-wise.
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


def resolve_anywhere(
    workspace: "WorkspaceRoot", tool_name: str, path: str
) -> "Path | ToolResult":
    """Resolve ``path`` for *reading* — unfenced.

    A **relative** path is joined onto the workspace and canonicalised, so
    the everyday form (``src/main.py``) behaves exactly as it always has. An
    **absolute** path is canonicalised and returned as-is: reads are not
    fenced, so a neighbouring checkout, a skill pack's bundled reference and
    a file under ``/usr/share`` are all reachable by naming them.

    A read is observation, not mutation; the fence that matters guards the
    irreversible acts (:func:`resolve_or_error`, used by ``edit`` / ``write``
    / ``apply_patch``). Anything the *host* must not disclose has to be kept
    off the host or out of the process — a path check inside the tool was
    never that boundary, since ``shell_run`` reads the same bytes (§B19).

    Only the malformed-argument case fails, so the shape stays
    ``Path | ToolResult`` like its fenced sibling.
    """
    if not isinstance(path, str) or not path:
        return tool_error(tool_name, "path must be a non-empty string")
    return workspace.canonicalise(path)
