"""``Edit`` / ``Write`` — exact-match replace and read-first whole-file write.

The write mode is **bound at construction** (``DRY_RUN`` / ``APPLY``) because
the Engine never pauses mid-flight to ask: confirmation is a pre-run policy, and
a dry run emits the unified diff as an artifact with nothing on disk moving.
Neither tool can clobber content the model has not seen: both consult the
session's :class:`FileReadRegistry` — the file must have been ``Read`` this
session AND its bytes must still match what was read. A successful mutation
re-records the new digest, so consecutive edits chain without a re-read.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.tool import FileReadRegistry, ToolContext, ToolResult
from noeta.tools.invocation import require_str, resolve_existing_file
from noeta.tools.limits import (
    SUMMARY_EMBED_MAX_BYTES,
    truncate_bytes,
)
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


#: Hard cap on a ``Write`` body — a runaway-file guard, far above any source
#: file a model legitimately authors in one call.
WRITE_FILE_MAX_BYTES = 8 * 1024 * 1024

#: Lines of surrounding context in the post-edit ``cat -n`` snippet.
_SNIPPET_CONTEXT_LINES = 4

#: Re-exported so callers can take the hash helper from this module.
_sha256 = file_hash


def _read_precondition_error(
    registry: Optional[FileReadRegistry],
    resolved: Path,
    raw: bytes,
    *,
    tool_name: str,
    verb: str,
) -> Optional[ToolResult]:
    """The read-first check both mutating tools share.

    The file must have been ``Read`` this session (a registry entry exists for
    the canonical path) and its current bytes must still hash to what was
    read — otherwise the model would mutate content it has not seen. ``None``
    registry ⇒ no enforcement (a hand-built ToolContext in a host test); the
    ToolRuntime always wires one.
    """
    if registry is None:
        return None
    recorded = registry.digest(str(resolved))
    if recorded is None:
        return tool_error(
            tool_name,
            f"File has not been read yet. Read it first before {verb} it.",
        )
    if recorded != hashlib.sha256(raw).hexdigest():
        return tool_error(
            tool_name,
            "File has been modified since it was read. Read it again before "
            f"{verb} it.",
        )
    return None


def _record_mutation(
    registry: Optional[FileReadRegistry], resolved: Path, body: bytes
) -> None:
    """Re-record the digest after a successful mutation, so consecutive edits
    chain without an intervening re-read (the model has seen exactly what it
    just wrote)."""
    if registry is not None:
        registry.record(str(resolved), hashlib.sha256(body).hexdigest())


def _cat_n_snippet(after_lines: list[str], center: int, span: int) -> str:
    """``cat -n`` rendering of the edited region ± context lines.

    ``center`` is the 0-based first changed line in the AFTER text; ``span``
    is how many lines the replacement occupies (0 for a pure deletion — the
    snippet then shows the seam).
    """
    lo = max(0, center - _SNIPPET_CONTEXT_LINES)
    hi = min(len(after_lines), center + max(span, 1) + _SNIPPET_CONTEXT_LINES)
    return "\n".join(
        f"{i + 1:>6}\t{after_lines[i]}" for i in range(lo, hi)
    )


def _first_changed_line(before: str, after: str) -> int:
    """0-based index of the first line where ``after`` differs from ``before``."""
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    for i, (b, a) in enumerate(zip(b_lines, a_lines)):
        if b != a:
            return i
    return min(len(b_lines), len(a_lines))


@dataclass
class ReplaceTextTool:
    """Replace a unique ``old_string`` in ``file_path`` with ``new_string``
    (tool name ``Edit``).

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
    name: str = "Edit"
    description: str = field(default=load_markdown(__package__, "edit"))
    # High risk so PermissionGuard treats this as privileged: a policy that
    # permits medium-risk tools must not accidentally allow file mutation.
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = lambda m: tool_error(self.name, m)  # noqa: E731
        path = require_str(
            arguments, "file_path", err, message="requires non-empty 'file_path'"
        )
        if isinstance(path, ToolResult):
            return path
        old = require_str(
            arguments, "old_string", err,
            message="requires non-empty 'old_string'",
        )
        if isinstance(old, ToolResult):
            return old
        new = arguments.get("new_string")
        if not isinstance(new, str):
            return tool_error(self.name, "requires string 'new_string'")
        if old == new:
            return tool_error(
                self.name, "old_string and new_string must be different"
            )
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

        precondition = _read_precondition_error(
            ctx.file_read_registry, resolved, raw,
            tool_name=self.name, verb="editing",
        )
        if precondition is not None:
            return precondition

        count = before.count(old)
        if count == 0:
            return tool_error(self.name, "String to replace not found in file.")
        # Without ``replace_all``, an ambiguous (N>1) match is refused so a
        # single edit can never silently touch a region the model did not
        # intend.
        if count > 1 and not replace_all:
            return tool_error(
                self.name,
                f"Found {count} matches of the string to replace, but "
                "replace_all is false. To replace all occurrences, set "
                "replace_all to true. To replace only one, provide more "
                "surrounding context to uniquely identify the instance.",
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
        after_bytes = after.encode("utf-8")
        if self.mode is FsWriteMode.APPLY:
            try:
                self.exec_env.write_bytes(resolved, after_bytes)
            except OSError as exc:
                return tool_error(self.name, f"write failed: {exc}")
            applied = True
            _record_mutation(ctx.file_read_registry, resolved, after_bytes)
            # The PRE-edit bytes are the ToolRuntime's rewind baseline for this
            # turn. ``Edit`` only ever touches an EXISTING file, so this is
            # never ``None`` — that marker means "did not exist" and only
            # ``Write`` can produce it.
            file_changes = [{"path": rel, "before": raw}]

        after_lines = after.splitlines()
        snippet = _cat_n_snippet(
            after_lines,
            _first_changed_line(before, after),
            len(new.splitlines()),
        )
        if applied:
            head = (
                f"The file {rel} has been updated. Here's the result of "
                "running `cat -n` on a snippet of the edited file:"
            )
        else:
            head = (
                f"Proposed edit to {rel} (dry run — nothing written). Here's "
                "a `cat -n` snippet of the result:"
            )
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        mode_label = "applied" if applied else "proposed"
        return ToolResult(
            success=True,
            output=f"{head}\n{snippet}",
            artifacts=[diff_ref],
            summary=f"edit {summary_path} +{added}/-{removed} ({mode_label})",
            file_changes=file_changes,
        )


@dataclass
class WriteFileTool:
    """Create a file, or overwrite one already read this session (tool name
    ``Write``).

    Creating a brand-new file always works — you cannot have read what did not
    exist — and missing parent directories are created. Overwriting an
    **existing** file is gated by the read-first precondition, so the model can
    never blindly clobber a file it has not seen.

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
    name: str = "Write"
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
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
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
            arguments, "file_path", lambda m: tool_error(self.name, m),
            message="requires non-empty 'file_path'",
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
            precondition = _read_precondition_error(
                ctx.file_read_registry, resolved, existing_raw,
                tool_name=self.name, verb="overwriting",
            )
            if precondition is not None:
                return precondition
            try:
                before_text = existing_raw.decode("utf-8")
            except UnicodeDecodeError:
                return tool_error(self.name, f"{path!r} is not utf-8 text")

        rel = workspace.relative(resolved)
        diff = compute_diff(before_text, content, rel)
        diff_ref = ctx.artifact_store.put(
            diff.encode("utf-8"), media_type=DIFF_MEDIA_TYPE
        )
        added, removed = diff_stat_counts(diff)

        applied = False
        file_changes: list[dict[str, Any]] | None = None
        if self.mode is FsWriteMode.APPLY:
            parent = resolved.parent
            if not self.exec_env.is_dir(parent):
                # Missing parents are created, not errored: a brand-new module
                # usually starts with its first file.
                try:
                    self.exec_env.mkdir(parent)
                except OSError as exc:
                    return tool_error(self.name, f"mkdir failed: {exc}")
            try:
                self.exec_env.write_bytes(resolved, body)
            except OSError as exc:
                return tool_error(self.name, f"write failed: {exc}")
            applied = True
            _record_mutation(ctx.file_read_registry, resolved, body)
            file_changes = [{"path": rel, "before": before_bytes}]

        if applied:
            output = (
                f"The file {rel} has been updated."
                if overwrite
                else f"File created successfully at: {rel}"
            )
        else:
            verb_label = "overwrite of" if overwrite else "write to"
            output = (
                f"Proposed {verb_label} {rel} (dry run — nothing written; "
                f"{len(body)} bytes, +{added}/-{removed})."
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
