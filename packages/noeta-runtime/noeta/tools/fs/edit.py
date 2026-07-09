"""Edit primitives + dry-run/apply mode + diff audit.

``edit(path, old, new)`` and ``write(path, content)`` are the **only**
ways the fs tool pack can modify the workspace. They share two hard
rules baked into the type-level contract:

* The write mode is **bound at construction** (``FsWriteMode.DRY_RUN`` or
  ``FsWriteMode.APPLY``). The Engine never pauses mid-flight to ask;
  confirmation is a *pre-run* policy set by the CLI (I4) / daemon
  governance. In dry-run, the tool computes the proposed change, emits
  the unified diff as a ContentStore artifact, and returns
  ``applied=False`` — nothing on disk moves.
* The fs tools' write side is *only* exercised inside a live
  ``Engine.run_one_step``.

The diff itself is computed with ``difflib.unified_diff``; the inline
``output`` carries before/after sha256 hashes + ``+N/-M`` counts + a
``diff_ref`` (the ``{hash,size,media_type}`` JSON form, never a raw
``ContentRef``) so the SPA / inspector can fetch the full diff out of
the ContentStore via the I6 artifact endpoint.

``edit`` requires its ``old`` segment to match the file **exactly once**
(B5) — 0 or >1 matches return ``success=False`` with **no write**.
``write`` creates a new file freely, but overwriting an **existing**
file is gated by a **read-first precondition**: the model
must have ``read`` that file's current contents earlier in this session,
so it can never blindly clobber a file it has not seen. The precondition
reuses the content-addressed ``ContentStore`` (``read`` offloads the full
file body keyed by its sha256; ``write`` checks that the existing file's
current bytes are present under that key) — **no new runtime primitive,
no new tool field**. A brand-new file has no read
precondition (you cannot read a file that does not exist). The body is
capped at ``WRITE_FILE_MAX_BYTES`` (64 KB) so v1 cannot land a runaway
file.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from noeta.protocols.errors import ContentNotFound
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.values import ContentRef
from noeta.tools._invocation import require_str, resolve_existing_file
from noeta.tools._limits import (
    INLINE_OUTPUT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools._refs import ref_json
from noeta.tools.descriptions import load_tool_description
from noeta.tools.fs._diff import (
    DIFF_MEDIA_TYPE,
    compute_diff,
    diff_stat_counts,
    file_hash,
)
from noeta.tools.fs._workspace import (
    WorkspaceRoot,
    resolve_or_error,
    tool_error,
)
from noeta.tools.fs.exec_env import ExecEnv, LocalExecEnv


__all__ = [
    "FsWriteMode",
    "ReplaceTextTool",
    "WRITE_FILE_MAX_BYTES",
    "WriteFileTool",
]


#: Hard cap on a ``write`` body. v1 safety bound; large new files
#: are deferred to Phase 6 along with multi-file patches.
WRITE_FILE_MAX_BYTES = 65_536

#: The media type ``read`` offloads file bodies under. ``write``'s
#: read-first precondition reconstructs the same content-addressed
#: ``ContentRef`` to ask "has this exact body been read this session?".
#: Must match ``noeta.tools.fs.read._READ_FILE_MEDIA_TYPE``.
_READ_FILE_MEDIA_TYPE = "text/plain"

#: Backward-compat alias — the diff primitives now live in
#: ``noeta.tools.fs._diff``. Kept so a caller importing ``_sha256`` from
#: this module (e.g. ``test_fs_apply_patch``) is not broken.
_sha256 = file_hash


def _was_read_this_session(ctx: ToolContext, raw: bytes) -> bool:
    """Whether ``raw`` (an existing file's current bytes) was ``read`` this
    session.

    read-first precondition, implemented with **zero new
    runtime primitives**: ``read`` offloads the full file body into the
    content-addressed ``ContentStore`` keyed by its sha256 (see
    ``ReadFileTool``). So "was this file read?" reduces to "are these exact
    bytes already in the store?" — reconstruct the ``ContentRef`` ``read``
    would have minted and probe ``get`` (``get`` is hash-only; ``size`` /
    ``media_type`` are not validated). A hit means the model saw this
    content; a miss (``ContentNotFound``) means it never read it.

    The probe hash is ``sha256`` over the **raw bytes** — the exact key the
    ``ContentStore`` computed at ``put`` time (``read`` offloads the raw
    file body, not a re-encoded string), so this matches even for files
    whose bytes are not a clean utf-8 round-trip.
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


class FsWriteMode(str, Enum):
    """Pre-run write policy passed to the edit tools at construction.

    ``DRY_RUN`` produces the proposed diff artifact + ``applied=False``;
    ``APPLY`` performs the write. The Engine never sees this enum — the
    decision lives entirely on the closure-injected ``FsToolPack``.
    """

    DRY_RUN = "dry_run"
    APPLY = "apply"


@dataclass
class ReplaceTextTool:
    """Replace a unique ``old`` segment in ``path`` with ``new`` (tool name
    ``edit``).

    The match must be exactly-once — 0 or N>1 matches return
    ``success=False`` and **never** write. This is the only Phase-4 way
    to edit existing files; there is intentionally no unified-diff
    applier (Q2 — too easy to land an invalid patch on a moved file).
    """

    workspace: WorkspaceRoot
    mode: FsWriteMode = FsWriteMode.DRY_RUN
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "edit"
    description: str = field(default=load_tool_description("edit"))
    # PRD D2: write-side fs tools are high-risk so PermissionGuard treats
    # them as privileged. A policy that permits medium-risk tools must
    # not accidentally allow file mutation.
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
        resolved = resolve_existing_file(
            self.workspace, self.name, path, exec_env=self.exec_env
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
        # ``replace_all`` relaxes the exactly-once contract to "replace every
        # match" — the only way to mutate N>1 matches in one call. Without it
        # an ambiguous (N>1) match is still refused so a single edit can never
        # silently touch a region the model did not intend.
        if count > 1 and not replace_all:
            return tool_error(
                self.name, f"'old' matches {count} times in {path!r}; must be unique"
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        rel = self.workspace.relative(resolved)
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
            # surface the PRE-edit bytes so the ToolRuntime
            # can stash this turn's rewind baseline. ``edit`` only ever touches
            # an EXISTING file (it just read ``raw``), so ``before`` is its old
            # content — never ``None`` (that marker is for AI-created files,
            # which only ``write`` can produce).
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

    Creating a brand-new file always works. Overwriting an **existing**
    file is gated by the read-first precondition: the model
    must have ``read`` that file's current contents earlier in this
    session (checked via the content-addressed store — no new primitive),
    otherwise the write is refused. The body is capped at
    ``WRITE_FILE_MAX_BYTES`` (64 KB) — a safety bound, not a UX cap.
    Multi-file patches and large blobs are deferred to Phase 6.

    **Path-restricted variant.**
    ``allowed_path_globs`` is an injected, construction-time
    whitelist of workspace-relative glob patterns. The empty default ⇒ the
    unrestricted ``write`` (current behaviour, byte-identical). When it is
    non-empty, a write whose resolved workspace-relative path matches NONE of
    the globs is refused with a path-guard error **before** any IO or
    read-first check. This is how a custom ``AgentSpec`` can physically confine
    a writer to e.g. ``plans/*.md``: the restriction is an assembly-time
    injection on the concrete tool object (same shape as ``mode`` /
    ``workspace``), NOT a new ``Tool`` / ``ToolRef`` / ``AgentSpec`` identity
    field (D1: zero new tool fields). The tool stays ``risk_level=high``
    regardless — a restricted write is still a privileged file mutation.
    """

    workspace: WorkspaceRoot
    mode: FsWriteMode = FsWriteMode.DRY_RUN
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "write"
    description: str = field(default=load_tool_description("write"))
    # PRD D2: write-side fs tools are high-risk so PermissionGuard treats
    # them as privileged. A policy that permits medium-risk tools must
    # not accidentally allow file creation.
    risk_level: str = "high"
    #: injected path whitelist (workspace-relative globs).
    #: Empty ⇒ unrestricted (default; identical to pre-0040-issue-04 builds).
    #: Non-empty ⇒ only paths matching one of these globs may be written
    #: (an AgentSpec may inject e.g. ``("plans/*.md",)``). Normalised to a
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
        whitelist. Empty whitelist ⇒ always allowed (unrestricted)."""
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
        resolved = resolve_or_error(self.workspace, self.name, path)
        if isinstance(resolved, ToolResult):
            return resolved
        # path guard: when this write was built with a path
        # whitelist (the ``plan`` preset's restricted write), refuse any path
        # outside it BEFORE the read-first check or any IO. The check runs on
        # the canonical workspace-relative form so ``..``/symlink escapes are
        # already collapsed by ``resolve``.
        rel_guard = self.workspace.relative(resolved)
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
        # the PRE-write bytes for the rewind baseline: the existing
        # content when overwriting, ``None`` when creating a brand-new file (the
        # "did not exist" marker → a rewind past this turn DELETES the file).
        before_bytes: bytes | None = None
        if overwrite:
            if not self.exec_env.is_file(resolved):
                return tool_error(self.name, f"not a file: {path!r}")
            try:
                existing_raw = self.exec_env.read_bytes(resolved)
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            before_bytes = existing_raw
            # overwriting an existing file requires you to have
            # ``read`` its CURRENT contents earlier this session — otherwise
            # the write is a blind clobber. (A brand-new file has no such
            # precondition; you cannot have read what did not exist.)
            if not _was_read_this_session(ctx, existing_raw):
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

        rel = self.workspace.relative(resolved)
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
            # surface the PRE-write bytes (or the ``None``
            # "did-not-exist" marker for a new file) so the ToolRuntime stashes
            # this turn's rewind baseline.
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
