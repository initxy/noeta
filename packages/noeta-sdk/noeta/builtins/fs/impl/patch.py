"""``apply_patch`` — a batch of edits carried by one tool call, applied atomically.

The whole batch is validated before anything is written and an apply-phase fault
rolls the workspace back to its pre-batch state, so a multi-file change never
lands half-done. Atomicity is a **tool-layer, in-process** property: the
rollback snapshots live in memory, so a hard OS crash mid-apply is not
protected, and the transaction boundary is one tool call ⇒ one
``PermissionGuard`` decision ⇒ one approval. Several ``replace`` edits may
target the same file — they fold into one in-memory buffer in array order — but
two path spellings that coincide only under case-fold / NFC are rejected,
because they are not one file on every filesystem.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.limits import INLINE_OUTPUT_MAX_BYTES
from noeta.tools.refs import ref_json
from noeta.protocols.resources import load_markdown
from noeta.builtins.fs.impl._diff import (
    DIFF_MEDIA_TYPE,
    compute_diff,
    diff_stat_counts,
    file_hash,
)
from noeta.runtime.workspace import (
    WorkspaceEscape,
    WorkspaceRoot,
    WriteRootsResolver,
    authorized_workspace,
)
from noeta.runtime.exec_env import ExclusiveCreateError, ExecEnv, LocalExecEnv
from noeta.builtins.fs.impl.edit import (
    FsWriteMode,
    WRITE_FILE_MAX_BYTES,
)


__all__ = [
    "ApplyPatchTool",
    "MAX_PATCH_CANONICAL_BYTES",
    "MAX_PATCH_EDITS",
]


#: Outer per-call sanity backstop on the whole ``arguments`` (canonical bytes).
#: Oversize args are offloaded to the ContentStore, so this is not an
#: envelope-fit guard — it only tells an absurdly large batch to split, and sits
#: well above any realistic multi-file batch.
MAX_PATCH_CANONICAL_BYTES = 256 * 1024
#: One tool call = one approval = one atomic batch; bounds the blast
#: radius / rollback scope per approval, not the byte size.
MAX_PATCH_EDITS = 16
_PATH_MAX_BYTES = 120
#: ``replace`` old/new and ``create`` content share ``write``'s file ceiling —
#: a safety bound, not a UX cap.
_OLDNEW_MAX_BYTES = WRITE_FILE_MAX_BYTES
_CONTENT_MAX_BYTES = WRITE_FILE_MAX_BYTES
_SHA_MAX_BYTES = 64


def _err(message: str) -> ToolResult:
    return ToolResult(success=False, summary=f"apply_patch: {message}")


def _b(value: str) -> int:
    return len(value.encode("utf-8"))


def _collision_key(resolved: Path) -> str:
    """A path identity key that catches exact, **unicode-normalization**
    (NFC), and **case** collisions — so two edits that would hit the same
    file on a case-insensitive / normalizing filesystem are rejected and
    behaviour does not silently diverge across platforms."""
    return unicodedata.normalize("NFC", str(resolved)).casefold()


@dataclass
class _Planned:
    op: str
    resolved: Path
    rel: str
    before_bytes: Optional[bytes]  # None ⇒ create (file does not exist)
    after_bytes: bytes
    before_sha256: str
    after_sha256: str
    added: int
    removed: int
    diff: str


@dataclass
class _Target:
    """A parsed edit — op/path validated and resolved to a workspace path, not
    yet read or planned. It keeps the original ``edit`` dict and its array index
    so all edits hitting one file can be grouped and planned together."""

    i: int
    edit: dict[str, Any]
    resolved: Path
    rel: str


@dataclass
class ApplyPatchTool:
    """Validate a batch of edits, then atomically apply (or dry-run) it.

    ``replace`` is an exact, unique substring swap and ``create`` writes a whole
    file; the unified diff is output and audit only, never an input, so there is
    no fuzzy partial apply.
    """

    workspace: WorkspaceRoot
    mode: FsWriteMode = FsWriteMode.DRY_RUN
    #: Host authorization for writes OUTSIDE the workspace, resolved per call
    #: (see ``authorized_workspace``). ``None`` ⇒ single-root wall.
    write_roots: Optional[WriteRootsResolver] = None
    #: File-IO backend for read / stat / replace-write / rollback. Path
    #: resolution stays on ``workspace``.
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "apply_patch"
    description: str = field(default=load_markdown(__package__, "apply_patch"))
    risk_level: str = "high"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "maxItems": MAX_PATCH_EDITS,
                    "items": {
                        "type": "object",
                        "properties": {
                            # maxLength is an LLM HINT (chars), not the real
                            # bound — the tool enforces UTF-8 byte caps plus a
                            # canonical-byte budget.
                            "op": {"type": "string", "enum": ["replace", "create"]},
                            "path": {"type": "string", "maxLength": _PATH_MAX_BYTES},
                            "old": {"type": "string", "maxLength": _OLDNEW_MAX_BYTES},
                            "new": {"type": "string", "maxLength": _OLDNEW_MAX_BYTES},
                            "content": {
                                "type": "string",
                                "maxLength": _CONTENT_MAX_BYTES,
                            },
                            "before_sha256": {
                                "type": "string",
                                "maxLength": _SHA_MAX_BYTES,
                            },
                        },
                        "required": ["op", "path"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["edits"],
            "additionalProperties": False,
        }
    )

    # -- invoke ----------------------------------------------------------

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if len(to_canonical_bytes(arguments)) > MAX_PATCH_CANONICAL_BYTES:
            return _err(
                f"patch too large (> {MAX_PATCH_CANONICAL_BYTES} canonical bytes); "
                "split into smaller apply_patch calls"
            )
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits:
            return _err("requires a non-empty 'edits' list")
        if len(edits) > MAX_PATCH_EDITS:
            return _err(f"too many edits ({len(edits)} > {MAX_PATCH_EDITS})")

        # ---- Phase A: validate every edit in memory (no writes, no puts).
        # Two DIFFERENT path spellings that coincide only under case-fold / NFC
        # are rejected: their on-disk identity diverges between case-sensitive
        # and case-insensitive filesystems, so silently merging them would not
        # be portable.
        groups: dict[str, list[_Target]] = {}
        order: list[str] = []                 # exact-key first-appearance order
        collide_owner: dict[str, str] = {}    # fold/NFC key -> owning exact key
        workspace = authorized_workspace(self.workspace, self.write_roots, ctx)
        for i, edit in enumerate(edits):
            tgt = self._resolve_target(workspace, i, edit)
            if isinstance(tgt, ToolResult):
                return tgt
            exact = str(tgt.resolved)
            fold = _collision_key(tgt.resolved)
            owner = collide_owner.setdefault(fold, exact)
            if owner != exact:
                return _err(
                    f"edit #{i}: target path {tgt.rel!r} collides with another "
                    "edit only under case-fold / unicode-normalization — "
                    "different spellings of one file are rejected (they are not "
                    "the same file on every filesystem)"
                )
            if exact not in groups:
                groups[exact] = []
                order.append(exact)
            groups[exact].append(tgt)

        planned: list[_Planned] = []
        for exact in order:
            res = self._plan_group(groups[exact])
            if isinstance(res, ToolResult):
                return res
            planned.append(res)

        # Resume-stable order. Each file's intra-group edit sequence is already
        # baked into its single _Planned, so sorting across files is safe.
        planned.sort(key=lambda p: p.resolved.as_posix())

        # ---- Phase B: apply (APPLY only) with TOCTOU revalidation + rollback.
        applied_flag = False
        if self.mode is FsWriteMode.APPLY:
            outcome = self._apply(planned)
            if outcome is not None:
                return outcome  # apply failure / rollback (typed)
            applied_flag = True

        # ---- Success: NOW put artifacts (none written before this point).
        return self._result(planned, ctx, applied=applied_flag)

    # -- Phase A helpers -------------------------------------------------

    def _resolve_target(
        self, workspace: WorkspaceRoot, i: int, edit: Any
    ) -> "_Target | ToolResult":
        """Validate op/path and resolve it against ``workspace`` — this call's
        fence, i.e. the workspace plus any host-authorized write roots."""
        if not isinstance(edit, dict):
            return _err(f"edit #{i} must be an object")
        op = edit.get("op")
        path = edit.get("path")
        if op not in ("replace", "create"):
            return _err(f"edit #{i}: op must be 'replace' or 'create'")
        if not isinstance(path, str) or not path:
            return _err(f"edit #{i}: requires non-empty 'path'")
        if _b(path) > _PATH_MAX_BYTES:
            return _err(f"edit #{i}: path exceeds {_PATH_MAX_BYTES} UTF-8 bytes")
        try:
            resolved = workspace.resolve(path)
        except WorkspaceEscape as exc:
            return _err(f"edit #{i}: {exc}")
        return _Target(
            i=i, edit=edit, resolved=resolved,
            rel=workspace.relative(resolved),
        )

    def _plan_group(self, members: list["_Target"]) -> "_Planned | ToolResult":
        """Plan one file's edits into a single ``_Planned``: either a lone
        ``create`` or one-or-more ``replace`` edits applied in order."""
        first = members[0]
        if any(m.edit.get("op") == "create" for m in members):
            if len(members) > 1:
                return _err(
                    f"edit #{first.i}: {first.rel!r} mixes 'create' with other "
                    "edits — a create writes the whole file; give the final "
                    "content in one create, or use 'replace' for every edit"
                )
            return self._plan_create(first)
        return self._plan_replaces(first.resolved, first.rel, members)

    def _plan_replaces(
        self, resolved: Path, rel: str, members: list["_Target"]
    ) -> "_Planned | ToolResult":
        """Apply every ``replace`` in ``members`` (array order) to one buffer:
        edit N's ``old`` is matched against the file as left by edits 0..N-1 in
        THIS call, and the result is one _Planned spanning original → final."""
        i0 = members[0].i
        if not self.exec_env.is_file(resolved):
            return _err(f"edit #{i0}: not a file: {rel!r}")
        try:
            before_bytes = self.exec_env.read_bytes(resolved)
            before = before_bytes.decode("utf-8")
        except OSError as exc:
            return _err(f"edit #{i0}: read failed: {exc}")
        except UnicodeDecodeError:
            return _err(f"edit #{i0}: {rel!r} is not utf-8 text")
        before_hash = file_hash(before)

        working = before
        for pos, m in enumerate(members):
            i = m.i
            edit = m.edit
            old = edit.get("old")
            new = edit.get("new")
            if not isinstance(old, str) or not old:
                return _err(f"edit #{i}: replace requires non-empty 'old'")
            if not isinstance(new, str):
                return _err(f"edit #{i}: replace requires string 'new'")
            if _b(old) > _OLDNEW_MAX_BYTES or _b(new) > _OLDNEW_MAX_BYTES:
                return _err(
                    f"edit #{i}: old/new exceeds {_OLDNEW_MAX_BYTES}B "
                    "(write safety cap); split into smaller replaces"
                )
            # Only a non-empty hash arms the staleness guard: models habitually
            # fill an optional string field with "" instead of omitting it, and
            # comparing a blank hash would fail every such edit as "stale".
            # Every before_sha256 in a group describes the ORIGINAL file — all
            # edits are authored against the same read — so they compare against
            # ``before_hash``, not the running buffer.
            want_sha = edit.get("before_sha256")
            if want_sha:
                if not isinstance(want_sha, str):
                    return _err(f"edit #{i}: before_sha256 must be a string")
                if _b(want_sha) > _SHA_MAX_BYTES:
                    return _err(f"edit #{i}: before_sha256 exceeds {_SHA_MAX_BYTES} bytes")
                if before_hash != want_sha:
                    return _err(
                        f"edit #{i}: {rel!r} before_sha256 mismatch (stale edit); "
                        "omit before_sha256 unless you copied it from a recent read "
                        "of this exact file"
                    )
            count = working.count(old)
            # Past the first edit, an earlier edit in THIS call may have already
            # rewritten the region, so the model is told to re-anchor.
            prior = (
                " (an earlier edit in this same call may have changed this region)"
                if pos else ""
            )
            if count == 0:
                return _err(
                    f"edit #{i}: 'old' not found in {rel!r}; copy it verbatim from "
                    "the file (watch indentation, trailing spaces, and newlines)"
                    + prior
                )
            if count > 1:
                return _err(
                    f"edit #{i}: 'old' matches {count}x in {rel!r}; must be unique — "
                    "include more surrounding context" + prior
                )
            working = working.replace(old, new, 1)

        after = working
        after_bytes = after.encode("utf-8")
        diff = compute_diff(before, after, rel)
        added, removed = diff_stat_counts(diff)
        return _Planned(
            op="replace", resolved=resolved, rel=rel,
            before_bytes=before_bytes, after_bytes=after_bytes,
            before_sha256=before_hash, after_sha256=file_hash(after),
            added=added, removed=removed, diff=diff,
        )

    def _plan_create(self, target: "_Target") -> "_Planned | ToolResult":
        i, resolved, rel = target.i, target.resolved, target.rel
        content = target.edit.get("content")
        if not isinstance(content, str):
            return _err(f"edit #{i}: create requires string 'content'")
        body = content.encode("utf-8")
        if len(body) > _CONTENT_MAX_BYTES:
            return _err(
                f"edit #{i}: content exceeds {_CONTENT_MAX_BYTES}B "
                "(write safety cap); split or write less"
            )
        if self.exec_env.exists(resolved):
            return _err(f"edit #{i}: path already exists: {rel!r} (use replace)")
        if not self.exec_env.is_dir(resolved.parent):
            return _err(f"edit #{i}: parent directory not found for {rel!r}")
        diff = compute_diff("", content, rel)
        added, removed = diff_stat_counts(diff)
        return _Planned(
            op="create", resolved=resolved, rel=rel,
            before_bytes=None, after_bytes=body,
            before_sha256=file_hash(""), after_sha256=file_hash(content),
            added=added, removed=removed, diff=diff,
        )

    # -- Phase B helper --------------------------------------------------

    def _apply(self, planned: list[_Planned]) -> Optional[ToolResult]:
        """Apply all edits with TOCTOU revalidation + exclusive create.

        Returns None on success, or a typed failure ToolResult after recovering
        the **current failing target** AND rolling back every already-written
        edit. ``recover`` tells the failure handler how to undo the current
        target: ``none`` (never modified — TOCTOU before any write, or a failed
        exclusive open), ``restore`` (a replace whose write may have truncated
        or partially written), ``delete`` (a create whose file exists because
        the exclusive open succeeded)."""
        done: list[_Planned] = []
        for p in planned:
            if p.op == "replace":
                try:
                    current = self.exec_env.read_bytes(p.resolved)
                except OSError as exc:
                    return self._fail(done, failed=p, recover="none",
                                      reason=f"read failed: {exc}")
                if current != p.before_bytes:
                    # TOCTOU — current target NOT modified yet → recover="none".
                    return self._fail(done, failed=p, recover="none",
                                      reason="file changed since validation (TOCTOU)")
                try:
                    self.exec_env.write_bytes(p.resolved, p.after_bytes)
                except OSError as exc:
                    # The file may now be truncated / partially written.
                    return self._fail(done, failed=p, recover="restore",
                                      reason=f"write failed: {exc}")
            else:  # create — exclusive, never overwrite
                # The atomic exclusive create lives behind the ExecEnv seam, so
                # its typed error carries the recover verb and reason rather
                # than this branch reconstructing them from an OSError.
                try:
                    self.exec_env.create_exclusive(p.resolved, p.after_bytes)
                except ExclusiveCreateError as exc:
                    return self._fail(
                        done, failed=p, recover=exc.recover, reason=exc.reason
                    )
            done.append(p)
        return None

    def _fail(
        self,
        done: list[_Planned],
        *,
        failed: _Planned,
        recover: str,
        reason: str,
    ) -> ToolResult:
        """Recover the current failing target (per ``recover``) AND restore
        every already-applied edit (reverse order). Any recovery write that
        itself fails is surfaced in ``rollback_failed`` — never claim
        byte-identity when recovery did not fully succeed."""
        rollback_failed: list[str] = []

        def _undo(p: _Planned) -> None:
            try:
                if p.op == "replace":
                    assert p.before_bytes is not None
                    self.exec_env.write_bytes(p.resolved, p.before_bytes)
                else:
                    self.exec_env.unlink(p.resolved)
            except OSError:
                rollback_failed.append(p.rel)

        # 1) the current failing target (it is NOT in `done`).
        if recover == "restore":
            try:
                assert failed.before_bytes is not None
                self.exec_env.write_bytes(failed.resolved, failed.before_bytes)
            except OSError:
                rollback_failed.append(failed.rel)
        elif recover == "delete":
            try:
                self.exec_env.unlink(failed.resolved)
            except OSError:
                rollback_failed.append(failed.rel)
        # 2) everything already applied, newest first.
        for p in reversed(done):
            _undo(p)

        out: dict[str, Any] = {
            "applied": False,
            "failed": {"path": failed.rel, "phase": "apply", "reason": reason},
        }
        if rollback_failed:
            out["rolled_back"] = False
            out["rollback_failed"] = rollback_failed
            summary = (
                f"apply_patch FAILED on {failed.rel} ({reason}); "
                f"ROLLBACK INCOMPLETE: {rollback_failed}"
            )
        else:
            out["rolled_back"] = True
            summary = f"apply_patch failed on {failed.rel} ({reason}); rolled back"
        return ToolResult(success=False, output=out, summary=summary)

    # -- result (only path that writes artifacts) ------------------------

    def _result(
        self, planned: list[_Planned], ctx: ToolContext, *, applied: bool
    ) -> ToolResult:
        artifacts = []
        for p in planned:
            artifacts.append(
                ctx.artifact_store.put(p.diff.encode("utf-8"), media_type=DIFF_MEDIA_TYPE)
            )
        combined = "".join(p.diff for p in planned)
        combined_ref = ctx.artifact_store.put(
            combined.encode("utf-8"), media_type=DIFF_MEDIA_TYPE
        )
        rows = [
            {
                "op": p.op,
                "path": p.rel,
                "applied": applied,
                "before_sha256": p.before_sha256,
                "after_sha256": p.after_sha256,
                "added": p.added,
                "removed": p.removed,
            }
            for p in planned
        ]
        output: dict[str, Any] = {
            "applied": applied,
            "edits": rows,
            "combined_diff_ref": ref_json(combined_ref),
        }
        output = _fit_output(output)
        mode_label = "applied" if applied else "proposed"
        total_added = sum(p.added for p in planned)
        total_removed = sum(p.removed for p in planned)
        # The ToolRuntime's rewind baseline for every path this call mutated.
        # ``before_bytes`` is already ``None`` for a ``create`` and the original
        # bytes for a ``replace`` — the same "did-not-exist" vs. "overwrote"
        # marker ``edit`` / ``write`` use. DRY_RUN mutates nothing, so it
        # surfaces no baseline either.
        file_changes: list[dict[str, Any]] | None = None
        if applied:
            file_changes = [
                {"path": p.rel, "before": p.before_bytes} for p in planned
            ]
        return ToolResult(
            success=True,
            output=output,
            artifacts=artifacts + [combined_ref],
            summary=(
                f"apply_patch {len(planned)} file(s) "
                f"+{total_added}/-{total_removed} ({mode_label})"
            ),
            file_changes=file_changes,
        )


def _fit_output(output: dict[str, Any]) -> dict[str, Any]:
    """Keep ``encoded_len(output) <= INLINE_OUTPUT_MAX_BYTES`` by trimming
    trailing ``edits`` rows (full record stays in the combined-diff
    artifact). Sets ``truncated`` when rows are dropped."""
    if len(to_canonical_bytes(output)) <= INLINE_OUTPUT_MAX_BYTES:
        return output
    rows = list(output.get("edits") or [])
    total = len(rows)
    while rows and len(to_canonical_bytes({**output, "edits": rows})) > INLINE_OUTPUT_MAX_BYTES:
        rows.pop()
    output = {**output, "edits": rows, "truncated": True, "total_edits": total}
    return output
