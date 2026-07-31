"""``edit`` / ``write`` — exact-match replace and read-first whole-file write.

The write mode is **bound at construction** (``DRY_RUN`` / ``APPLY``) because
the Engine never pauses mid-flight to ask: confirmation is a pre-run policy, and
a dry run emits the unified diff as an artifact with ``applied=False`` while
nothing on disk moves. Neither tool can clobber content the model has not seen —
``edit`` refuses unless its ``old`` segment matches **exactly once**, and
overwriting an existing file requires that file's current bytes to have been
``read`` earlier in the session, a precondition served by probing the
content-addressed store rather than by any new runtime primitive or tool field.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.values import ContentRef
from noeta.tools.invocation import require_str, resolve_existing_file
from noeta.tools.limits import (
    INLINE_OUTPUT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools.refs import ref_json
from noeta.protocols.resources import load_markdown
from noeta.builtins.fs.impl._diff import (
    DIFF_MEDIA_TYPE,
    compute_diff,
    diff_stat_counts,
    file_hash,
)
from noeta.runtime.workspace import (
    FsWriteMode,
    WorkspaceRoot,
    WriteRootsResolver,
    authorized_workspace,
    resolve_or_error,
    tool_error,
)
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv


__all__ = [
    "FsWriteMode",
    "ReplaceTextTool",
    "WRITE_FILE_MAX_BYTES",
    "WriteFileTool",
]


#: Hard cap on a ``write`` body — a safety bound against a runaway file.
WRITE_FILE_MAX_BYTES = 65_536

#: The media type ``read`` offloads file bodies under. ``write``'s read-first
#: precondition reconstructs the same content-addressed ``ContentRef`` to ask
#: "has this exact body been read this session?", so this must stay identical
#: to ``read``'s own ``_READ_FILE_MEDIA_TYPE``.
_READ_FILE_MEDIA_TYPE = "text/plain"

#: Re-exported so callers can take the hash helper from this module.
_sha256 = file_hash


def _was_read_this_task(ctx: ToolContext, raw: bytes) -> bool:
    """Whether ``raw`` (an existing file's current bytes) was ``read`` this
    session.

    ``read`` offloads every file body into the content-addressed store, so
    "was this file read?" reduces to "are these exact bytes already stored?" —
    reconstruct the ``ContentRef`` ``read`` would have minted and probe ``get``,
    which keys on the hash alone. The hash must be taken over the **raw bytes**,
    the same key ``put`` computed, so the probe still matches a file whose bytes
    are not a clean utf-8 round-trip.
    """
    probe = ContentRef(
        hash=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        media_type=_READ_FILE_MEDIA_TYPE,
    )
    try:
        ctx.artifact_store.get(probe)
    except ContentNotFound:
        return False
    return True


@dataclass
class ReplaceTextTool:
    """Replace a unique ``old`` segment in ``path`` with ``new`` (tool name
    ``edit``).

    The match must be exactly-once — 0 or N>1 matches return
    ``success=False`` and **never** write. There is deliberately no
    unified-diff applier: it is too easy to land an invalid patch on a file
    that has shifted underneath it.
    """

    workspace: WorkspaceRoot
    mode: FsWriteMode = FsWriteMode.DRY_RUN
    #: Host authorization for writes OUTSIDE the workspace, resolved per call
    #: (see ``authorized_workspace``). ``None`` ⇒ single-root wall.
    write_roots: Optional[WriteRootsResolver] = None
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "edit"
    description: str = field(default=load_markdown(__package__, "edit"))
    # High risk so PermissionGuard treats this as privileged: a policy that
    # permits medium-risk tools must not accidentally allow file mutation.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = lambda m: tool_error(self.name, m)  # noqa: E731
        path = require_str(arguments, "path", err, message="requires non-empty 'path'")
        if isinstance(path, ToolResult):
            return path
        old = require_str(arguments, "old", err, message="requires non-empty 'old'")
        if isinstance(old, ToolResult):
            return old
        new = arguments.get("new")
        if not isinstance(new, str):
            return tool_error(self.name, "requires string 'new'")
        replace_all = bool(arguments.get("replace_all"))
        workspace = authorized_workspace(self.workspace, self.write_roots, ctx)
        resolved = resolve_existing_file(
            workspace, self.name, path, exec_env=self.exec_env
        )
        if isinstance(resolved, ToolResult):
            return resolved
        try:
            raw = self.exec_env.read_bytes(resolved)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        try:
            before = raw.decode("utf-8")
        except UnicodeDecodeError:
            return tool_error(self.name, f"{path!r} is not utf-8 text")

        count = before.count(old)
        if count == 0:
            return tool_error(self.name, f"'old' not found in {path!r}")
        # Without ``replace_all``, an ambiguous (N>1) match is refused so a
        # single edit can never silently touch a region the model did not
        # intend.
        if count > 1 and not replace_all:
            return tool_error(
                self.name, f"'old' matches {count} times in {path!r}; must be unique"
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        rel = workspace.relative(resolved)
        diff = compute_diff(before, after, rel)
        diff_ref = ctx.artifact_store.put(
            diff.encode("utf-8"), media_type=DIFF_MEDIA_TYPE
        )
        added, removed = diff_stat_counts(diff)

        applied = False
        file_changes: list[dict[str, Any]] | None = None
        if self.mode is FsWriteMode.APPLY:
            try:
                self.exec_env.write_bytes(resolved, after.encode("utf-8"))
            except OSError as exc:
                return tool_error(self.name, f"write failed: {exc}")
            applied = True
            # The PRE-edit bytes are the ToolRuntime's rewind baseline for this
            # turn. ``edit`` only ever touches an EXISTING file, so this is
            # never ``None`` — that marker means "did not exist" and only
            # ``write`` can produce it.
            file_changes = [{"path": rel, "before": raw}]

        output: dict[str, Any] = {
            "path": rel,
            "applied": applied,
            "before_sha256": file_hash(before),
            "after_sha256": file_hash(after),
            "added": added,
            "removed": removed,
            "diff_ref": ref_json(diff_ref),
        }
        output = fit_output_fields(
            output, shrink_order=["path"], max_bytes=INLINE_OUTPUT_MAX_BYTES
        )
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        mode_label = "applied" if applied else "proposed"
        return ToolResult(
            success=True,
            output=output,
            artifacts=[diff_ref],
            summary=f"edit {summary_path} +{added}/-{removed} ({mode_label})",
            file_changes=file_changes,
        )


@dataclass
class WriteFileTool:
    """Create a new file, or overwrite one already read this session (tool
    name ``write``).

    Creating a brand-new file always works — you cannot have read what did not
    exist. Overwriting an **existing** file is gated by the read-first
    precondition, so the model can never blindly clobber a file it has not
    seen, and the body is capped at ``WRITE_FILE_MAX_BYTES``.

    ``allowed_path_globs`` is how an ``AgentSpec`` physically confines a writer
    to e.g. ``plans/*.md``: a construction-time injection on the concrete tool
    object, in the same shape as ``mode`` / ``workspace``, rather than a new
    ``Tool`` / ``ToolRef`` / ``AgentSpec`` identity field. The tool stays
    ``risk_level=high`` regardless — a restricted write is still a privileged
    file mutation.
    """

    workspace: WorkspaceRoot
    mode: FsWriteMode = FsWriteMode.DRY_RUN
    #: Host authorization for writes OUTSIDE the workspace, resolved per call
    #: (see ``authorized_workspace``). ``None`` ⇒ single-root wall.
    write_roots: Optional[WriteRootsResolver] = None
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "write"
    description: str = field(default=load_markdown(__package__, "write"))
    # High risk so PermissionGuard treats this as privileged: a policy that
    # permits medium-risk tools must not accidentally allow file creation.
    risk_level: str = "high"
    #: Workspace-relative glob whitelist; empty ⇒ unrestricted. Normalised to a
    #: sorted tuple so two equal whitelists compare equal.
    allowed_path_globs: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }
    )

    def __post_init__(self) -> None:
        self.allowed_path_globs = tuple(sorted(self.allowed_path_globs))

    def _path_allowed(self, rel: str) -> bool:
        """Whether workspace-relative ``rel`` is writable under the injected
        whitelist. Empty whitelist ⇒ always allowed."""
        if not self.allowed_path_globs:
            return True
        return any(fnmatch.fnmatch(rel, pat) for pat in self.allowed_path_globs)

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = require_str(
            arguments, "path", lambda m: tool_error(self.name, m),
            message="requires non-empty 'path'",
        )
        if isinstance(path, ToolResult):
            return path
        content = arguments.get("content")
        if not isinstance(content, str):
            return tool_error(self.name, "requires string 'content'")
        body = content.encode("utf-8")
        if len(body) > WRITE_FILE_MAX_BYTES:
            return tool_error(
                self.name,
                f"content {len(body)}B exceeds {WRITE_FILE_MAX_BYTES}B cap",
            )
        workspace = authorized_workspace(self.workspace, self.write_roots, ctx)
        resolved = resolve_or_error(workspace, self.name, path)
        if isinstance(resolved, ToolResult):
            return resolved
        # The whitelist is enforced BEFORE the read-first check or any IO, on
        # the canonical workspace-relative form so ``..`` / symlink escapes are
        # already collapsed by ``resolve``.
        rel_guard = workspace.relative(resolved)
        if not self._path_allowed(rel_guard):
            allowed = ", ".join(self.allowed_path_globs)
            return tool_error(
                self.name,
                f"path {rel_guard!r} is outside the writable allow-list "
                f"({allowed}); this agent may only write matching paths",
            )
        # ``exists()`` follows symlinks, but ``resolve`` has already
        # canonicalised, so an existing symlink resolves to the target's
        # path which is checked here.
        overwrite = self.exec_env.exists(resolved)
        before_text = ""
        # The ToolRuntime's rewind baseline: existing content when overwriting,
        # ``None`` for a brand-new file — that marker makes a rewind past this
        # turn DELETE the file.
        before_bytes: bytes | None = None
        if overwrite:
            if not self.exec_env.is_file(resolved):
                return tool_error(self.name, f"not a file: {path!r}")
            try:
                existing_raw = self.exec_env.read_bytes(resolved)
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            before_bytes = existing_raw
            # Overwriting requires having ``read`` the file's CURRENT contents
            # earlier this session; otherwise the write is a blind clobber.
            if not _was_read_this_task(ctx, existing_raw):
                return tool_error(
                    self.name,
                    f"must read {path!r} before overwriting it "
                    "(read-first precondition)",
                )
            try:
                before_text = existing_raw.decode("utf-8")
            except UnicodeDecodeError:
                return tool_error(self.name, f"{path!r} is not utf-8 text")
        else:
            parent = resolved.parent
            if not self.exec_env.is_dir(parent):
                return tool_error(
                    self.name, f"parent directory not found for {path!r}"
                )

        rel = workspace.relative(resolved)
        diff = compute_diff(before_text, content, rel)
        diff_ref = ctx.artifact_store.put(
            diff.encode("utf-8"), media_type=DIFF_MEDIA_TYPE
        )
        added, removed = diff_stat_counts(diff)

        applied = False
        file_changes: list[dict[str, Any]] | None = None
        if self.mode is FsWriteMode.APPLY:
            try:
                self.exec_env.write_bytes(resolved, body)
            except OSError as exc:
                return tool_error(self.name, f"write failed: {exc}")
            applied = True
            file_changes = [{"path": rel, "before": before_bytes}]

        output: dict[str, Any] = {
            "path": rel,
            "applied": applied,
            "before_sha256": file_hash(before_text),
            "after_sha256": file_hash(content),
            "bytes": len(body),
            "added": added,
            "removed": removed,
            "diff_ref": ref_json(diff_ref),
        }
        output = fit_output_fields(
            output, shrink_order=["path"], max_bytes=INLINE_OUTPUT_MAX_BYTES
        )
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        mode_label = "applied" if applied else "proposed"
        verb = "overwrite" if overwrite else "write"
        return ToolResult(
            success=True,
            output=output,
            artifacts=[diff_ref],
            summary=(
                f"{verb} {summary_path} +{added}/-{removed} "
                f"({len(body)}B, {mode_label})"
            ),
            file_changes=file_changes,
        )
