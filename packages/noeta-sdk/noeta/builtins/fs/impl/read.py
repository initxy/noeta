"""Read-only fs tools: ``Read`` / ``Glob`` / ``Grep``.

A read is observation, not mutation, so paths resolve **unfenced**: a relative
path is anchored on the shared ``WorkspaceRoot``, an absolute one is read where
it points. Every failure degrades to ``ToolResult(success=False, …)`` so a
malformed argument or a missing file cannot crash the worker. Outputs are the
plain text the model reads directly — ``Read`` in ``cat -n`` form, ``Glob`` as
a path list, ``Grep`` in ripgrep-style lines; the full file body still goes to
the ContentStore as the audit artifact, never into the model-facing text.
"""

from __future__ import annotations

import hashlib
import os
import re

try:  # CPython's regex AST walker (sre_parse → re._parser in 3.11)
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - <3.11 fallback
    import sre_parse as _re_parser  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.invocation import (
    require_str,
    resolve_readable_file,
)
from noeta.tools.limits import (
    INLINE_CONTENT_MAX_BYTES,
    INLINE_OUTPUT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
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


# Display caps: a longer underlying result is truncated with a notice so the
# model knows to narrow its query.
_MAX_GLOB_MATCHES = 200
_MAX_GREP_MATCHES = 50
#: Max bytes of any single line fed to ``regex.search``. Nested-quantifier
#: patterns are already rejected up front; this bounds the linear-but-heavy
#: case so a single very long line can't dominate a scan.
_MAX_GREP_SCAN_LINE_BYTES = 8192
_DEFAULT_READ_LIMIT = 2000  # lines
#: Per-line visible-char cap. A minified file can be one multi-MB line, which
#: would otherwise dominate the whole inline budget; the untouched body is
#: always available as the artifact.
_MAX_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"
_READ_FILE_MEDIA_TYPE = "text/plain"

#: Directory names never walked by ``Glob`` / ``Grep`` (dependency and cache
#: trees). Hidden directories (leading ``.``) are skipped by a separate rule,
#: so this set only needs the un-dotted offenders. An explicitly targeted
#: ``path`` is still searched — the filter applies to the walk beneath the
#: search root, not to the root itself.
_SKIPPED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "venv",
        "dist",
        "build",
        "target",
    }
)

#: ``re`` opcodes that backtrack; POSSESSIVE_REPEAT / atomic groups do not.
_BACKTRACKING_REPEATS = frozenset(
    {_re_parser.MAX_REPEAT, _re_parser.MIN_REPEAT}
)

#: The parser's "unbounded" upper-bound sentinel (``*`` / ``+`` / ``{n,}``).
_MAXREPEAT = getattr(_re_parser, "MAXREPEAT", 4294967295)


# ``Read`` cannot know whether the bound model is vision-capable —
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

#: Leading magic bytes → wire media type for the bytes ``Read`` surfaces to a
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


def _clip_line(line: str) -> str:
    """Cap one (ending-free) line's visible chars with a truncation marker."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + _LINE_TRUNC_MARKER


def _reminder(text: str) -> str:
    """A host-authored inline notice, in the envelope the model is trained to
    read as ambient context rather than file content."""
    return f"<system-reminder>{text}</system-reminder>"


def _walk_is_skipped(rel_parts: tuple[str, ...]) -> bool:
    """Whether a walked entry (parts relative to the search root) is skipped:
    anything under a hidden or dependency/cache directory, or itself hidden."""
    return any(
        part.startswith(".") or part in _SKIPPED_DIRS for part in rel_parts
    )


@dataclass
class ReadFileTool:
    """Read a file's contents in ``cat -n`` form, optionally line-sliced
    (tool name ``Read``).

    ``offset`` is 1-based, matching common editor conventions. The full body is
    always offloaded as a ContentStore artifact (the audit record), and the
    read is registered with the session's :class:`FileReadRegistry` — the
    record ``Edit`` / ``Write`` consult for their read-first precondition.
    """

    workspace: WorkspaceRoot
    #: Backend for the file read (local host or a sandbox container). Path
    #: *resolution* stays on ``workspace``, which for ``Read`` only anchors
    #: relative paths — reads are unfenced, so an absolute path is read where it
    #: points: a neighbouring checkout, a skill pack's bundled reference.
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "Read"
    description: str = field(default=load_markdown(__package__, "read"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        }
    )

    def _display(self, resolved: Path) -> str:
        """Workspace-relative POSIX path, or the absolute POSIX path for a
        read that landed outside the workspace."""
        return self.workspace.relative(resolved)

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = require_str(
            arguments, "file_path", lambda m: tool_error(self.name, m),
            message="requires non-empty 'file_path'",
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
                output=f"Read image {rel} ({image_media_type}, {len(raw)} bytes)",
                summary=f"read {summary_path} (image, {len(raw)} bytes)",
                images=[ref],
            )

        # A NUL byte marks real binary regardless of decodability (NUL is a
        # "valid" UTF-8 code point, but no text file carries one).
        if b"\x00" in raw:
            return tool_error(self.name, f"{path!r} is not utf-8 text")
        try:
            full_text = raw.decode("utf-8")
            bytes_replaced = False
        except UnicodeDecodeError:
            # A text file with stray invalid bytes (legacy encodings, BOM
            # remnants) — decode leniently so the file stays readable instead
            # of failing outright.
            full_text = raw.decode("utf-8", errors="replace")
            bytes_replaced = True

        # The artifact is always the FULL file body, independent of the sliced
        # view returned inline, so the recorded artifact hash stays stable
        # across different slices.
        ref = ctx.artifact_store.put(raw, media_type=_READ_FILE_MEDIA_TYPE)
        # Register the read for the Edit/Write read-first precondition. Keyed
        # by the canonical absolute path — the same form those tools resolve to
        # — and digesting the RAW bytes, so a later mutation check compares
        # like with like.
        if ctx.file_read_registry is not None:
            ctx.file_read_registry.record(
                str(resolved), hashlib.sha256(raw).hexdigest()
            )

        rel = self._display(resolved)
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        note = "; non-utf8 bytes replaced" if bytes_replaced else ""

        lines = full_text.splitlines()
        total_lines = len(lines)
        if total_lines == 0:
            return ToolResult(
                success=True,
                output=_reminder(
                    "Warning: the file exists but has empty contents."
                ),
                artifacts=[ref],
                summary=f"read {summary_path} (empty{note})",
            )
        start = offset - 1
        if start >= total_lines:
            return ToolResult(
                success=True,
                output=_reminder(
                    f"Warning: the file has only {total_lines} lines, fewer "
                    f"than the requested offset {offset}."
                ),
                artifacts=[ref],
                summary=f"read {summary_path} (offset past end{note})",
            )
        end = min(start + limit, total_lines)

        # cat -n form: right-aligned 1-based line number, a tab, the line. The
        # inline byte ceiling is a safety fence only — trim whole lines from
        # the end until the rendering fits, and say so.
        numbered = [
            f"{start + i + 1:>6}\t{_clip_line(line)}"
            for i, line in enumerate(lines[start:end])
        ]
        shown_end = end
        rendered = "\n".join(numbered)
        while numbered and len(rendered.encode("utf-8")) > INLINE_CONTENT_MAX_BYTES:
            drop = max(1, len(numbered) // 4)
            numbered = numbered[:-drop]
            shown_end = start + len(numbered)
            rendered = "\n".join(numbered)

        notes: list[str] = []
        if bytes_replaced:
            notes.append(
                _reminder("Non-utf8 bytes were replaced with U+FFFD.")
            )
        if shown_end < total_lines or start > 0:
            notes.append(
                _reminder(
                    f"Showing lines {start + 1}-{shown_end} of {total_lines} "
                    f"total lines. Use offset={shown_end + 1} to continue "
                    "reading."
                )
            )
        output = rendered
        if notes:
            output = output + "\n\n" + "\n".join(notes)

        return ToolResult(
            success=True,
            output=output,
            artifacts=[ref],
            summary=(
                f"read {summary_path} "
                f"(lines {start + 1}–{shown_end} of {total_lines}{note})"
            ),
        )


def _looks_relative(pattern: str) -> bool:
    """Patterns must be relative to the searched root; anchor / / abs / .. is
    rejected.

    ``Path.glob`` itself supports absolute patterns on POSIX, but an absolute
    pattern is *unbounded*: ``/**/*.py`` walks the whole filesystem, and the
    match cap applies only after the walk has already happened. Scoping is the
    ``path`` argument's job (a named tree, bounded by construction), never the
    pattern's — the same division ``Grep`` already draws.
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
    """Match a glob pattern under one directory (tool name ``Glob``).

    Optional ``path`` chooses the tree to search — workspace-relative, or
    absolute to search outside it, resolved unfenced like ``Grep``'s.
    ``pattern`` stays relative to that root, which is what keeps every walk
    bounded. Results are newline-separated POSIX paths sorted by modification
    time (newest first), capped with a notice.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "Glob"
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

        entries: list[tuple[float, str]] = []
        for match in raw_matches:
            # The searched tree may contain symlinks, so a match whose real path
            # escapes the tree that was asked about is dropped. Checked against
            # the search root rather than the workspace, or an out-of-workspace
            # search would discard every result.
            resolved = self.workspace.canonicalise(os.fspath(match))
            if not path_within(resolved, root):
                continue
            try:
                rel_parts = resolved.relative_to(root).parts
            except ValueError:  # pragma: no cover - path_within already held
                continue
            if _walk_is_skipped(rel_parts):
                continue
            entries.append(
                (self.exec_env.mtime(resolved), self.workspace.relative(resolved))
            )
        # Newest first, alphabetical tiebreak — recency is the model's usual
        # relevance signal when it scans a truncated list top-down.
        entries.sort(key=lambda e: (-e[0], e[1]))
        total = len(entries)
        shown = [rel for _, rel in entries[:_MAX_GLOB_MATCHES]]

        if not shown:
            output = "No files found"
        else:
            output = "\n".join(shown)
            if total > len(shown):
                output += (
                    f"\n(Results truncated: showing {len(shown)} of {total} "
                    "matches, newest first. Narrow the pattern.)"
                )
        summary_pat = truncate_bytes(pattern, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            summary=f"glob {summary_pat!r}: {len(shown)} of {total} match(es)",
        )


_GREP_OUTPUT_MODES = ("files_with_matches", "content", "count")


def _optional_count(arguments: dict[str, Any], key: str) -> "int | None | str":
    """A non-negative int argument, ``None`` when absent, error text when bad."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"'{key}' must be a non-negative integer"
    return value


@dataclass
class GrepTool:
    """Regex search across the workspace or a sub-tree (tool name ``Grep``).

    ``pattern`` is a Python ``re`` regex, screened for catastrophic
    backtracking before it runs (grep executes in-process on the engine worker
    thread, where a GIL-holding pathological match could not be preempted).
    Output modes, case folding, context lines and ``head_limit`` follow the
    reference agent's surface; the walk skips hidden and dependency/cache
    directories and silently skips unreadable or binary files.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "Grep"
    description: str = field(default=load_markdown(__package__, "grep"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "output_mode": {
                    "type": "string",
                    "enum": list(_GREP_OUTPUT_MODES),
                },
                "-i": {"type": "boolean"},
                "-n": {"type": "boolean"},
                "-A": {"type": "integer", "minimum": 0},
                "-B": {"type": "integer", "minimum": 0},
                "-C": {"type": "integer", "minimum": 0},
                "head_limit": {"type": "integer", "minimum": 1},
                "multiline": {"type": "boolean"},
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

        mode = arguments.get("output_mode", "files_with_matches")
        if mode not in _GREP_OUTPUT_MODES:
            return tool_error(
                self.name,
                "'output_mode' must be one of: " + ", ".join(_GREP_OUTPUT_MODES),
            )
        case_insensitive = bool(arguments.get("-i"))
        line_numbers = bool(arguments.get("-n"))
        multiline = bool(arguments.get("multiline"))
        after = _optional_count(arguments, "-A")
        if isinstance(after, str):
            return tool_error(self.name, after)
        before = _optional_count(arguments, "-B")
        if isinstance(before, str):
            return tool_error(self.name, before)
        around = _optional_count(arguments, "-C")
        if isinstance(around, str):
            return tool_error(self.name, around)
        head_limit_raw = arguments.get("head_limit")
        if head_limit_raw is not None and (
            isinstance(head_limit_raw, bool)
            or not isinstance(head_limit_raw, int)
            or head_limit_raw < 1
        ):
            return tool_error(self.name, "'head_limit' must be a positive integer")
        head_limit: Optional[int] = head_limit_raw
        ctx_before = around if around is not None else (before or 0)
        ctx_after = around if around is not None else (after or 0)

        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.MULTILINE | re.DOTALL
        try:
            regex = re.compile(pattern, flags)
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

        if mode == "files_with_matches":
            return self._run_files_mode(candidates, regex, multiline, head_limit)
        if mode == "count":
            return self._run_count_mode(candidates, regex, multiline, head_limit)
        return self._run_content_mode(
            candidates,
            regex,
            multiline=multiline,
            line_numbers=line_numbers,
            ctx_before=ctx_before,
            ctx_after=ctx_after,
            head_limit=head_limit,
        )

    # -- matching helpers ---------------------------------------------------

    def _match_lines(
        self, text: str, regex: "re.Pattern[str]", multiline: bool
    ) -> list[int]:
        """0-based line numbers containing a match (a multiline match reports
        every line its span covers)."""
        if not multiline:
            return [
                i
                for i, line in enumerate(text.splitlines())
                if regex.search(
                    truncate_bytes(line, _MAX_GREP_SCAN_LINE_BYTES)
                )
            ]
        hits: list[int] = []
        line_starts = _line_start_offsets(text)
        seen: set[int] = set()
        for m in regex.finditer(text):
            first = _offset_to_line(line_starts, m.start())
            last = _offset_to_line(
                line_starts, max(m.start(), m.end() - 1)
            )
            for ln in range(first, last + 1):
                if ln not in seen:
                    seen.add(ln)
                    hits.append(ln)
        return hits

    def _iter_matching(
        self,
        candidates: list[Path],
        regex: "re.Pattern[str]",
        multiline: bool,
    ) -> Iterable[tuple[str, str, list[int]]]:
        """Yield ``(rel_path, text, matching 0-based line numbers)`` per file
        with at least one match, skipping unreadable / non-utf8 files."""
        for file_path in candidates:
            try:
                text = self.exec_env.read_text(file_path, encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "\x00" in text:  # NUL ⇒ binary, even though it decodes
                continue
            lines = self._match_lines(text, regex, multiline)
            if lines:
                yield self.workspace.relative(file_path), text, lines

    def _run_files_mode(
        self,
        candidates: list[Path],
        regex: "re.Pattern[str]",
        multiline: bool,
        head_limit: Optional[int],
    ) -> ToolResult:
        cap = head_limit if head_limit is not None else _MAX_GLOB_MATCHES
        paths: list[str] = []
        truncated = False
        for rel, _text, _lines in self._iter_matching(
            candidates, regex, multiline
        ):
            if len(paths) >= cap:
                truncated = True
                break
            paths.append(rel)
        if not paths:
            return ToolResult(
                success=True, output="No matches found", summary="grep: 0 files"
            )
        output = "\n".join(paths)
        if truncated:
            output += (
                f"\n(Results truncated: first {cap} files shown. Narrow the "
                "query or raise head_limit.)"
            )
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep: {len(paths)} file(s)",
        )

    def _run_count_mode(
        self,
        candidates: list[Path],
        regex: "re.Pattern[str]",
        multiline: bool,
        head_limit: Optional[int],
    ) -> ToolResult:
        cap = head_limit if head_limit is not None else _MAX_GLOB_MATCHES
        rows: list[str] = []
        total = 0
        truncated = False
        for rel, _text, lines in self._iter_matching(
            candidates, regex, multiline
        ):
            if len(rows) >= cap:
                truncated = True
                break
            rows.append(f"{rel}:{len(lines)}")
            total += len(lines)
        if not rows:
            return ToolResult(
                success=True, output="No matches found", summary="grep: 0 matches"
            )
        output = "\n".join(rows)
        if truncated:
            output += f"\n(Results truncated: first {cap} files shown.)"
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep: {total} match(es) in {len(rows)} file(s)",
        )

    def _run_content_mode(
        self,
        candidates: list[Path],
        regex: "re.Pattern[str]",
        *,
        multiline: bool,
        line_numbers: bool,
        ctx_before: int,
        ctx_after: int,
        head_limit: Optional[int],
    ) -> ToolResult:
        cap = min(head_limit, _MAX_GREP_MATCHES) if head_limit else _MAX_GREP_MATCHES
        blocks: list[str] = []
        shown = 0
        total = 0
        for rel, text, match_lines in self._iter_matching(
            candidates, regex, multiline
        ):
            total += len(match_lines)
            budget = cap - shown
            if budget <= 0:
                continue  # keep counting the total for the notice
            take = match_lines[:budget]
            shown += len(take)
            blocks.append(
                _render_content_block(
                    rel,
                    text,
                    take,
                    line_numbers=line_numbers,
                    ctx_before=ctx_before,
                    ctx_after=ctx_after,
                )
            )
        if shown == 0:
            return ToolResult(
                success=True, output="No matches found", summary="grep: 0 matches"
            )
        separator = "--\n" if (ctx_before or ctx_after) else ""
        output = ("\n" + separator).join(blocks)
        # The 32 KB inline budget is a fence, not the working rule — the match
        # cap keeps ordinary output far below it. Trim whole lines if a wall of
        # clipped-wide lines still overflows.
        out_lines = output.splitlines()
        while (
            len(out_lines) > 1
            and len("\n".join(out_lines).encode("utf-8")) > INLINE_OUTPUT_MAX_BYTES
        ):
            out_lines = out_lines[: max(1, len(out_lines) // 2)]
        output = "\n".join(out_lines)
        if shown < total:
            output += (
                f"\n(Showing {shown} of {total} matches. Narrow the query or "
                "use head_limit.)"
            )
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep: {shown} of {total} match(es)",
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
                rel_parts = entry.relative_to(resolved).parts
            except ValueError:
                continue
            if _walk_is_skipped(rel_parts):
                continue
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


def _line_start_offsets(text: str) -> list[int]:
    """Byte-free char offsets where each line starts, for span→line mapping."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset_to_line(line_starts: list[int], offset: int) -> int:
    """0-based line number containing char ``offset`` (binary search)."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _render_content_block(
    rel: str,
    text: str,
    match_lines: list[int],
    *,
    line_numbers: bool,
    ctx_before: int,
    ctx_after: int,
) -> str:
    """Render one file's matches in ripgrep style: ``path:line:content`` for
    match lines, ``path-line-content`` for context lines, contiguous ranges
    merged, ranges separated by ``--``."""
    lines = text.splitlines()
    total = len(lines)
    match_set = set(match_lines)
    # Merge each match's context window into maximal contiguous ranges.
    ranges: list[tuple[int, int]] = []
    for ln in sorted(match_set):
        lo = max(0, ln - ctx_before)
        hi = min(total - 1, ln + ctx_after)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))
    parts: list[str] = []
    for gi, (lo, hi) in enumerate(ranges):
        if gi > 0 and (ctx_before or ctx_after):
            parts.append("--")
        for ln in range(lo, hi + 1):
            body = _clip_line(lines[ln])
            is_match = ln in match_set
            sep = ":" if is_match else "-"
            if line_numbers:
                parts.append(f"{rel}{sep}{ln + 1}{sep}{body}")
            else:
                parts.append(f"{rel}{sep}{body}")
    return "\n".join(parts)
