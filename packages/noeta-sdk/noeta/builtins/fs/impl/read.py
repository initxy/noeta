"""Read-only fs tools: ``read`` / ``glob`` / ``grep``.

A read is observation, not mutation, so paths resolve **unfenced**: a relative
path is anchored on the shared ``WorkspaceRoot``, an absolute one is read where
it points. Every failure degrades to ``ToolResult(success=False, …)`` so a
malformed argument or a missing file cannot crash the worker, and results stay
inside the inline budget — a large ``read`` body is offloaded to the
ContentStore and the model gets a bounded excerpt plus a ref it can navigate
with ``offset`` / ``limit``.
"""

from __future__ import annotations

import os
import re

try:  # CPython's regex AST walker (sre_parse → re._parser in 3.11)
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - <3.11 fallback
    import sre_parse as _re_parser  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.invocation import (
    fit_dropping_tail,
    require_str,
    resolve_readable_file,
)
from noeta.tools.limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    encoded_len,
    fit_output_fields,
    truncate_bytes,
)
from noeta.protocols.resources import load_markdown
from noeta.runtime.workspace import (
    WorkspaceRoot,
    path_within,
    resolve_anywhere,
    tool_error,
)
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv


__all__ = [
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
]


# Display caps: a longer underlying result is reported as ``truncated=True``
# so the model knows to narrow its query.
_MAX_GLOB_MATCHES = 200
_MAX_GREP_MATCHES = 50
_MAX_GREP_LINE_BYTES = 400
#: Max bytes of any single line fed to ``regex.search``. Nested-quantifier
#: patterns are already rejected up front; this bounds the linear-but-heavy
#: case so a single very long line can't dominate a scan.
_MAX_GREP_SCAN_LINE_BYTES = 8192
_DEFAULT_READ_LIMIT = 2000  # lines
#: Per-line visible-char cap. A minified file can be one multi-MB line, which
#: would otherwise dominate the whole inline budget; the untouched body is
#: always available as the ``content_ref`` artifact.
_MAX_READ_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"
_READ_FILE_MEDIA_TYPE = "text/plain"

#: ``re`` opcodes that backtrack; POSSESSIVE_REPEAT / atomic groups do not.
_BACKTRACKING_REPEATS = frozenset(
    {_re_parser.MAX_REPEAT, _re_parser.MIN_REPEAT}
)

#: The parser's "unbounded" upper-bound sentinel (``*`` / ``+`` / ``{n,}``).
_MAXREPEAT = getattr(_re_parser, "MAXREPEAT", 4294967295)


# ``read`` cannot know whether the bound model is vision-capable —
# ``supports_vision`` lives on the catalog ``ModelSpec`` and is consulted inside
# the adapters at wire time, while ``ToolContext`` carries only task / trace ids
# — so it ALWAYS emits a detected image and lets the adapter degrade to text.
# PDFs (a separate wire shape) and images over ``IMAGE_MAX_BYTES`` instead fail
# with an actionable message; any other binary falls through to the generic
# "not utf-8" error below.

#: Leading magic bytes → a human-facing media label. Detection is by content,
#: not extension, so a mis-named or extension-less image is still caught.
#: ``webp`` needs a second check (RIFF container + ``WEBP`` fourcc) handled in
#: ``_detect_visual_media``.
_VISUAL_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
)

#: Leading magic bytes → wire media type for the bytes ``read`` surfaces to a
#: vision model. Deliberately excludes PDF, which is not an inline image.
_IMAGE_MAGIC_MEDIA_TYPE: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: Single-image inline ceiling. There is no image library to auto-downscale, so
#: a larger image degrades to text asking the model to crop or resize it rather
#: than blowing up the payload.
IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _detect_visual_media(raw: bytes) -> Optional[str]:
    """Return a media label if ``raw`` is an image (png/jpg/gif/webp) or a
    PDF, else ``None``.

    Sniffing by magic bytes rather than extension, so a ``.png`` renamed to
    ``.dat`` is still classified correctly. ``webp`` is a RIFF container, hence
    the extra ``WEBP`` fourcc check at offset 8.
    """
    for prefix, label in _VISUAL_MAGIC:
        if raw.startswith(prefix):
            return label
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "WebP image"
    return None


def _detect_image_media_type(raw: bytes) -> Optional[str]:
    """Return the standard image media type (``image/png`` …) if ``raw`` is a
    supported image (png/jpeg/gif/webp), else ``None``.

    A PDF is visual but not an inline image, so it yields ``None`` and the
    caller routes it to the degrade path.
    """
    for prefix, media_type in _IMAGE_MAGIC_MEDIA_TYPE:
        if raw.startswith(prefix):
            return media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _contains_repeat(node: Any) -> bool:
    """True if the parsed regex ``node`` contains a backtracking repeat at any depth."""
    if isinstance(node, _re_parser.SubPattern):
        for op, args in node:
            if op in _BACKTRACKING_REPEATS:
                return True
            if _contains_repeat(args):
                return True
        return False
    if isinstance(node, (tuple, list)):
        return any(_contains_repeat(x) for x in node)
    return False


def _has_nested_repeat(node: Any) -> bool:
    """True if a backtracking repeat has a body that itself repeats (any depth)."""
    if isinstance(node, _re_parser.SubPattern):
        for op, args in node:
            if op in _BACKTRACKING_REPEATS:
                body = args[2] if isinstance(args, tuple) and len(args) >= 3 else None
                if body is not None and _contains_repeat(body):
                    return True
            if _has_nested_repeat(args):
                return True
        return False
    if isinstance(node, (tuple, list)):
        return any(_has_nested_repeat(x) for x in node)
    return False


def _find_branches(node: Any) -> list[list]:
    """Collect every ``BRANCH``'s alternative-list reachable within ``node``."""
    found: list[list] = []
    if isinstance(node, _re_parser.SubPattern):
        for op, args in node:
            if op == _re_parser.BRANCH and isinstance(args, tuple) and len(args) == 2:
                found.append(args[1])
            found.extend(_find_branches(args))
    elif isinstance(node, (tuple, list)):
        for x in node:
            found.extend(_find_branches(x))
    return found


def _leading_literal_and_nullable(alt: Any) -> tuple[Optional[int], bool]:
    """``(fixed leading literal or None, nullable)`` for one alternation branch.

    Only a fixed leading ``LITERAL`` can prove two branches start differently,
    so anything wilder — char class, any, group, a repeat with a positive
    minimum — yields ``None``. Running out of tokens means the branch matches
    the empty string (``nullable``)."""
    if not isinstance(alt, (list, _re_parser.SubPattern)):
        return (None, False)
    for op, args in alt:
        if op == _re_parser.AT:            # zero-width anchor: keep scanning
            continue
        if op == _re_parser.LITERAL:
            return (args, False)
        if op in _BACKTRACKING_REPEATS:
            mn = args[0] if isinstance(args, tuple) else 0
            if mn == 0:                    # optional leading element: skip past
                continue
            return (None, False)
        return (None, False)               # class / any / group / branch → wild
    return (None, True)                    # nothing left → matches the empty string


def _alternation_is_overlap_prone(alternatives: list) -> bool:
    """A ``BRANCH``'s alternatives can overlap-match (→ exponential backtracking
    once that branch sits under an unbounded repeat) when a branch is nullable
    (matches empty) or two branches share a fixed leading literal.

    CPython's parser factors a shared prefix out of an alternation, so the
    classic "one alternative is a prefix of another" case (``a|ab``, ``aa|a``)
    reaches this check as a branch with an EMPTY (nullable) alternative."""
    leading: list[int] = []
    for alt in alternatives:
        lit, nullable = _leading_literal_and_nullable(alt)
        if nullable:
            return True
        if lit is not None:
            leading.append(lit)
    return len(leading) != len(set(leading))


def _has_overlapping_alternation_repeat(node: Any) -> bool:
    """True if an UNBOUNDED backtracking repeat wraps an overlap-prone
    alternation — the alternation-ambiguity class of ReDoS the nested-quantifier
    check misses (``(a|a)*``, ``(a|ab)*``, ``(a?|b)+``)."""
    if isinstance(node, _re_parser.SubPattern):
        for op, args in node:
            if (
                op in _BACKTRACKING_REPEATS
                and isinstance(args, tuple)
                and len(args) >= 3
                and args[1] == _MAXREPEAT
            ):
                for alts in _find_branches(args[2]):
                    if _alternation_is_overlap_prone(alts):
                        return True
            if _has_overlapping_alternation_repeat(args):
                return True
        return False
    if isinstance(node, (tuple, list)):
        return any(_has_overlapping_alternation_repeat(x) for x in node)
    return False


def _pattern_is_redos_prone(pattern: str) -> bool:
    """Reject the two structural necessary conditions for exponential
    backtracking:

    * **nested unbounded quantifiers** — ``(a+)+``, ``(.*)*``, ``(\\d+\\.)+`` …;
    * **an unbounded repeat over an overlapping alternation** — ``(a|a)*``,
      ``(a|ab)*``, ``(a?|b)+`` (the ambiguity class the nested check misses).

    grep runs in-process on the engine worker thread and CPython's ``re`` holds
    the GIL for the whole match, so a pathological pattern would freeze the
    entire process with no possible timeout — a separate thread cannot preempt a
    GIL-holding C match. Refusing the pattern up front is the only GIL-safe
    guard, and it is deliberately conservative: safe shapes like ``(a+b)+`` are
    rejected too, and the model is told to flatten the pattern.
    """
    try:
        parsed = _re_parser.parse(pattern)
    except re.error:  # pragma: no cover - compile guard already reported it
        return False
    return _has_nested_repeat(parsed) or _has_overlapping_alternation_repeat(parsed)


def _clip_line(line: str) -> tuple[str, bool]:
    """Cap one line's visible chars, preserving its trailing CR/LF.

    The marker lands before the line ending so the line count and the re-join
    stay intact.
    """
    body = line.rstrip("\r\n")
    if len(body) <= _MAX_READ_LINE_CHARS:
        return line, False
    ending = line[len(body):]
    return body[:_MAX_READ_LINE_CHARS] + _LINE_TRUNC_MARKER + ending, True


@dataclass
class ReadFileTool:
    """Read a file's contents, optionally line-sliced.

    ``offset`` is 1-based, matching common editor conventions. The full body is
    always offloaded as a ContentStore artifact, so an inline ``output`` that
    would exceed ``INLINE_CONTENT_MAX_BYTES`` can shrink to an excerpt plus a
    ref the model re-reads through with a narrower slice.
    """

    workspace: WorkspaceRoot
    #: Backend for the file read (local host or a sandbox container). Path
    #: *resolution* stays on ``workspace``, which for ``read`` only anchors
    #: relative paths — reads are unfenced, so an absolute path is read where it
    #: points: a neighbouring checkout, a skill pack's bundled reference.
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "read"
    description: str = field(default=load_markdown(__package__, "read"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        }
    )

    def _display(self, resolved: Path) -> str:
        """Workspace-relative POSIX path, or the absolute POSIX path for a
        read that landed outside the workspace."""
        return self.workspace.relative(resolved)

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = require_str(
            arguments, "path", lambda m: tool_error(self.name, m),
            message="requires non-empty 'path'",
        )
        if isinstance(path, ToolResult):
            return path
        resolved = resolve_readable_file(
            self.workspace, self.name, path, exec_env=self.exec_env,
        )
        if isinstance(resolved, ToolResult):
            return resolved

        offset_raw = arguments.get("offset")
        limit_raw = arguments.get("limit")
        offset = offset_raw if isinstance(offset_raw, int) and offset_raw > 0 else 1
        limit = (
            limit_raw
            if isinstance(limit_raw, int) and limit_raw > 0
            else _DEFAULT_READ_LIMIT
        )

        try:
            raw = self.exec_env.read_bytes(resolved)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")

        # A PDF or an over-limit image degrades with a precise message instead
        # of the misleading generic "not utf-8 text" error below.
        media_label = _detect_visual_media(raw)
        if media_label is not None:
            image_media_type = _detect_image_media_type(raw)
            if image_media_type is None:
                return tool_error(
                    self.name,
                    f"{path!r} is a {media_label}, not text — reading PDFs "
                    "into the conversation is not supported yet",
                )
            if len(raw) > IMAGE_MAX_BYTES:
                return tool_error(
                    self.name,
                    f"{path!r} is a {media_label} of {len(raw)} bytes, over "
                    f"the {IMAGE_MAX_BYTES // 1024 // 1024}MB inline limit — "
                    "crop/resize it smaller before reading",
                )
            ref = ctx.artifact_store.put(raw, media_type=image_media_type)
            rel = self._display(resolved)
            summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
            return ToolResult(
                success=True,
                output={
                    "path": rel,
                    "media_type": image_media_type,
                    "bytes": len(raw),
                },
                summary=f"read {summary_path} (image, {len(raw)} bytes)",
                images=[ref],
            )

        try:
            full_text = raw.decode("utf-8")
            bytes_replaced = False
        except UnicodeDecodeError:
            # A NUL byte marks real binary. Anything else is a text file with
            # stray invalid bytes (legacy encodings, BOM remnants) — decode
            # leniently so the file stays readable instead of failing outright.
            if b"\x00" in raw:
                return tool_error(self.name, f"{path!r} is not utf-8 text")
            full_text = raw.decode("utf-8", errors="replace")
            bytes_replaced = True

        # The artifact is always the FULL file body, independent of the sliced
        # view returned inline, so the ref stays passable to other tools and the
        # recorded artifact hash stays stable across different slices.
        ref = ctx.artifact_store.put(raw, media_type=_READ_FILE_MEDIA_TYPE)

        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)
        start = min(offset - 1, total_lines)
        end = min(start + limit, total_lines)
        clipped: list[str] = []
        line_truncated = False
        for line in lines[start:end]:
            text_line, was_clipped = _clip_line(line)
            clipped.append(text_line)
            line_truncated = line_truncated or was_clipped
        sliced = "".join(clipped)
        slice_truncated = end < total_lines or start > 0 or line_truncated

        rel = self._display(resolved)
        output: dict[str, Any] = {
            "path": rel,
            "content": sliced,
            "content_ref": {
                "hash": ref.hash,
                "size": ref.size,
                "media_type": ref.media_type,
            },
            "offset": offset,
            "lines_read": max(0, end - start),
            "total_lines": total_lines,
            "truncated": slice_truncated,
        }
        # Hard canonical byte ceiling: the full body is already the artifact, so
        # oversize ``content`` shrinks to an excerpt and the model navigates
        # with ``offset`` / ``limit`` or ``grep``.
        if encoded_len(output) > INLINE_CONTENT_MAX_BYTES:
            output["truncated"] = True
            output = fit_output_fields(
                output, shrink_order=["content"], max_bytes=INLINE_CONTENT_MAX_BYTES
            )
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        note = "; non-utf8 bytes replaced" if bytes_replaced else ""
        return ToolResult(
            success=True,
            output=output,
            artifacts=[ref],
            summary=(
                f"read {summary_path} "
                f"(lines {output['offset']}–{output['offset'] + output['lines_read'] - 1} "
                f"of {total_lines}{note})"
                if output["lines_read"] > 0
                else f"read {summary_path} (empty{note})"
            ),
        )


def _looks_relative(pattern: str) -> bool:
    """Patterns must be relative to the searched root; anchor / / abs / .. is
    rejected.

    ``Path.glob`` itself supports absolute patterns on POSIX, but an absolute
    pattern is *unbounded*: ``/**/*.py`` walks the whole filesystem, and the
    match cap applies only after the walk has already happened. Scoping is the
    ``path`` argument's job (a named tree, bounded by construction), never the
    pattern's — the same division ``grep`` already draws.
    """
    if not pattern:
        return False
    if pattern.startswith("/") or pattern.startswith(os.sep):
        return False
    if pattern.startswith(".."):
        return False
    return True


@dataclass
class GlobTool:
    """Match a glob pattern under one directory (``Path.glob`` semantics).

    Optional ``path`` chooses the tree to search — workspace-relative, or
    absolute to search outside it, resolved unfenced like ``grep``'s.
    ``pattern`` stays relative to that root, which is what keeps every walk
    bounded. Results are POSIX strings sorted for determinism and capped to
    ``_MAX_GLOB_MATCHES``.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "glob"
    description: str = field(default=load_markdown(__package__, "glob"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require_str(
            arguments, "pattern", lambda m: tool_error(self.name, m),
            message="requires non-empty 'pattern'",
        )
        if isinstance(pattern, ToolResult):
            return pattern
        if not _looks_relative(pattern):
            return tool_error(
                self.name,
                "pattern must be relative to the searched directory (no leading "
                "'/' / '..') — use 'path' to search another tree",
            )

        path_arg = arguments.get("path")
        if path_arg is None or path_arg == "":
            path_arg = "."
        if not isinstance(path_arg, str):
            return tool_error(self.name, "'path' must be a string")
        root = resolve_anywhere(self.workspace, self.name, path_arg)
        if isinstance(root, ToolResult):
            return root

        try:
            raw_matches = list(self.exec_env.glob(root, pattern))
        except (OSError, ValueError) as exc:
            return tool_error(self.name, f"glob failed: {exc}")

        relpaths: list[str] = []
        for match in raw_matches:
            # The searched tree may contain symlinks, so a match whose real path
            # escapes the tree that was asked about is dropped. Checked against
            # the search root rather than the workspace, or an out-of-workspace
            # search would discard every result.
            resolved = self.workspace.canonicalise(os.fspath(match))
            if not path_within(resolved, root):
                continue
            relpaths.append(self.workspace.relative(resolved))
        relpaths.sort()
        total = len(relpaths)
        matches = relpaths[:_MAX_GLOB_MATCHES]
        output: dict[str, Any] = {
            "pattern": truncate_bytes(pattern, 256),
            "matches": matches,
            "total": total,
            "truncated": total > _MAX_GLOB_MATCHES,
        }
        output = fit_dropping_tail(output, "matches")
        matches = output["matches"]
        summary_pat = truncate_bytes(pattern, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            summary=f"glob {summary_pat!r}: {len(matches)} of {total} match(es)",
        )


@dataclass
class GrepTool:
    """Regex search across the workspace (or a sub-directory / file).

    ``pattern`` is a Python ``re`` regex, screened for catastrophic
    backtracking before it runs. Long lines are truncated for inline display,
    the match count is capped at ``_MAX_GREP_MATCHES``, and unreadable or
    binary files are skipped silently rather than failing the whole search.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "grep"
    description: str = field(default=load_markdown(__package__, "grep"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require_str(
            arguments, "pattern", lambda m: tool_error(self.name, m),
            message="requires non-empty 'pattern'",
        )
        if isinstance(pattern, ToolResult):
            return pattern
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return tool_error(self.name, f"invalid regex: {exc}")
        if _pattern_is_redos_prone(pattern):
            return tool_error(
                self.name,
                "pattern risks catastrophic backtracking — nested quantifiers "
                "(e.g. '(a+)+') or an unbounded repeat over an overlapping "
                "alternation (e.g. '(a|a)*'); flatten it to a linear pattern",
            )

        path_arg = arguments.get("path")
        if path_arg is None or path_arg == "":
            path_arg = "."
        if not isinstance(path_arg, str):
            return tool_error(self.name, "'path' must be a string")
        resolved = resolve_anywhere(self.workspace, self.name, path_arg)
        if isinstance(resolved, ToolResult):
            return resolved

        glob_filter = arguments.get("glob")
        if glob_filter is not None and not isinstance(glob_filter, str):
            return tool_error(self.name, "'glob' must be a string")

        candidates = self._candidate_files(resolved, glob_filter)
        matches: list[dict[str, Any]] = []
        total_matches = 0
        for file_path in candidates:
            try:
                text = self.exec_env.read_text(file_path, encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = self.workspace.relative(file_path)
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(truncate_bytes(line, _MAX_GREP_SCAN_LINE_BYTES)):
                    total_matches += 1
                    if len(matches) < _MAX_GREP_MATCHES:
                        matches.append(
                            {
                                "path": rel,
                                "line_number": line_no,
                                "line": truncate_bytes(line, _MAX_GREP_LINE_BYTES),
                            }
                        )

        truncated = total_matches > len(matches)
        output: dict[str, Any] = {
            "pattern": truncate_bytes(pattern, 256),
            "matches": matches,
            "total": total_matches,
            "truncated": truncated,
        }
        output = fit_dropping_tail(output, "matches")
        matches = output["matches"]
        summary_pat = truncate_bytes(pattern, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep {summary_pat!r}: {len(matches)} of {total_matches} match(es)",
        )

    def _candidate_files(
        self, resolved: Path, glob_filter: Optional[str]
    ) -> list[Path]:
        if self.exec_env.is_file(resolved):
            return [resolved]
        if not self.exec_env.is_dir(resolved):
            return []
        if glob_filter:
            it = self.exec_env.glob(resolved, glob_filter)
        else:
            it = self.exec_env.rglob(resolved, "*")
        files: list[Path] = []
        for entry in it:
            try:
                # Skipping symlinks keeps the walk inside one physical tree, so
                # a link cannot make the same file appear twice or send an
                # rglob around a cycle. Reads are unfenced, so this is a
                # traversal rule, not a containment one — a caller who wants
                # the link's target greps the target directly.
                if self.exec_env.is_file(entry) and not self.exec_env.is_symlink(entry):
                    files.append(entry)
            except OSError:
                continue
        # Deterministic order, so a repeated search returns the same prefix
        # once the match cap bites.
        files.sort(key=lambda p: self.workspace.relative(p))
        return files
