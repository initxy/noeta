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

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional

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
    resolve_anywhere,
    tool_error,
)
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv
from noeta.runtime.subproc import RunOutcome


__all__ = [
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
]


# Display caps: a longer underlying result is truncated with a notice so the
# model knows to narrow its query.
_MAX_GLOB_MATCHES = 200
#: ``Grep``'s implicit ``head_limit`` when the model passes none — the
#: reference surface's default (an explicit 0 lifts it entirely).
_GREP_DEFAULT_HEAD_LIMIT = 250
_DEFAULT_READ_LIMIT = 2000  # lines
#: Per-line visible-char cap. A minified file can be one multi-MB line, which
#: would otherwise dominate the whole inline budget; the untouched body is
#: always available as the artifact.
_MAX_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"
_READ_FILE_MEDIA_TYPE = "text/plain"

#: ``Glob`` / ``Grep`` shell out to ripgrep through ``ExecEnv.run_argv`` — the
#: reference agent's engine, so the walk semantics (gitignore-aware, hidden
#: skipped, symlinks not followed, binary skipped) and the regex dialect
#: (linear-time, no lookaround/backreferences) are the ones the model was
#: trained on, identically on the local host and inside a sandbox container.
#: rg is a hard requirement of these two tools; a missing binary fails loud
#: with an install hint.
_RG_TIMEOUT_S = 60
#: Ceiling on captured rg output — far above what the display caps can show.
#: A capped ``--json`` stream ends mid-line; the decoder skips the partial
#: tail and renders the complete prefix.
_RG_OUTPUT_CAP = 16 * 1024 * 1024

_RG_MISSING = (
    "ripgrep (rg) is required but was not found in the execution environment "
    "— install it (https://github.com/BurntSushi/ripgrep) or add it to the "
    "sandbox image"
)


def _run_rg(exec_env: ExecEnv, argv: list[str], cwd: Path) -> "RunOutcome | str":
    """One bounded rg invocation; a ``str`` return is the human-facing failure."""
    try:
        outcome = exec_env.run_argv(
            argv, cwd=cwd, timeout_s=_RG_TIMEOUT_S, output_cap=_RG_OUTPUT_CAP
        )
    except OSError:
        return _RG_MISSING
    if outcome.timed_out:
        return (
            f"search timed out after {_RG_TIMEOUT_S}s — narrow the pattern "
            "or path"
        )
    if outcome.returncode == 127:  # how a sandbox shell reports a missing binary
        return _RG_MISSING  # pragma: no cover - container-only path
    return outcome


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


def _glob_segments(pattern: str) -> tuple[str, ...]:
    """Split a relative glob pattern into path segments, dropping no-ops."""
    return tuple(seg for seg in pattern.split("/") if seg not in ("", "."))


def _glob_match(parts: tuple[str, ...], pats: tuple[str, ...]) -> bool:
    """pathlib-style glob over path segments: ``*`` / ``?`` / ``[...]`` stay
    inside one segment, ``**`` spans zero or more segments."""
    if not pats:
        return not parts
    head, rest = pats[0], pats[1:]
    if head == "**":
        return any(_glob_match(parts[i:], rest) for i in range(len(parts) + 1))
    return (
        bool(parts)
        and fnmatch.fnmatchcase(parts[0], head)
        and _glob_match(parts[1:], rest)
    )


@dataclass
class _FileHits:
    """One file's rows decoded from the ``rg --json`` event stream."""

    rel: str
    #: ``(1-based line number, text, is_match)`` in stream order.
    rows: list[tuple[int, str, bool]]
    match_lines: int = 0


def _decode_rg_events(
    stdout: bytes,
    rel_of: Callable[[str], str],
    *,
    only_matching: bool = False,
) -> list[_FileHits]:
    """Fold ``rg --json`` events into per-file ordered line rows.

    A multiline match spans several lines and every spanned line counts as a
    match line; with ``only_matching`` each submatch becomes its own row
    (``rg -o``) and context events are dropped. Binary files never emit
    match events, so they drop out silently, and a non-utf8 path or line (rg
    encodes those as base64 ``bytes``) is skipped like the utf-8-only scan
    always skipped it. A cap-truncated stream just ends mid-line: the
    partial tail fails to parse and everything before it renders normally.
    """
    by_path: dict[str, _FileHits] = {}
    order: list[_FileHits] = []
    for raw in stdout.split(b"\n"):
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") not in ("match", "context"):
            continue
        data = event.get("data") or {}
        path_text = (data.get("path") or {}).get("text")
        text = (data.get("lines") or {}).get("text")
        first_line = data.get("line_number")
        if path_text is None or text is None or first_line is None:
            continue
        is_match = event["type"] == "match"
        if only_matching and not is_match:
            continue
        hits = by_path.get(path_text)
        if hits is None:
            hits = _FileHits(rel=rel_of(path_text), rows=[])
            by_path[path_text] = hits
            order.append(hits)
        if only_matching:
            for sub in data.get("submatches") or []:
                sub_text = (sub.get("match") or {}).get("text")
                if sub_text is None:
                    continue
                for j, part in enumerate(sub_text.splitlines() or [""]):
                    hits.rows.append((first_line + j, part, True))
                    hits.match_lines += 1
            continue
        for i, line in enumerate(text.splitlines() or [""]):
            hits.rows.append((first_line + i, line, is_match))
            if is_match:
                hits.match_lines += 1
    return order


def _clip_line(line: str) -> str:
    """Cap one (ending-free) line's visible chars with a truncation marker."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + _LINE_TRUNC_MARKER


def _reminder(text: str) -> str:
    """A host-authored inline notice, in the envelope the model is trained to
    read as ambient context rather than file content."""
    return f"<system-reminder>{text}</system-reminder>"


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
    bounded. The walk is ``rg --files`` (gitignore-aware, hidden skipped,
    symlinks not followed); the pattern then matches with pathlib glob
    semantics, so ``*.py`` stays top-level and ``**/*.py`` recurses. Results
    are newline-separated POSIX paths sorted by modification time (newest
    first), capped with a notice.
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
        if not self.exec_env.is_dir(root):
            # A missing or non-directory root yields the empty result, not an
            # rg usage error — probing a tree that may not exist is routine.
            return ToolResult(
                success=True,
                output="No files found",
                summary=f"glob {pattern!r}: 0 of 0 match(es)",
            )

        ran = _run_rg(
            self.exec_env,
            ["rg", "--files", "--no-config", "--no-messages", "--", str(root)],
            self.workspace.root,
        )
        if isinstance(ran, str):
            return tool_error(self.name, ran)
        # Exit 1 = clean walk, zero files. Exit 2 with a silent stderr is
        # per-entry IO noise (--no-messages) — the listing rg did produce
        # stands; only a spoken error (bad invocation) fails the call.
        reason = ran.stderr.decode("utf-8", errors="replace").strip()
        if ran.returncode not in (0, 1) and reason:
            return tool_error(self.name, f"rg: {reason}")

        pats = _glob_segments(pattern)
        entries: list[tuple[float, str]] = []
        for line in ran.stdout.decode("utf-8", errors="replace").splitlines():
            match = Path(line)
            try:
                rel_parts = match.relative_to(root).parts
            except ValueError:  # pragma: no cover - rg stays under its root
                continue
            if not _glob_match(rel_parts, pats):
                continue
            try:
                entries.append(
                    (self.exec_env.mtime(match), self.workspace.relative(match))
                )
            except OSError:  # pragma: no cover - deleted mid-walk
                continue
        # Newest first, alphabetical tiebreak — recency is the model's usual
        # relevance signal when it scans a truncated list top-down.
        entries.sort(key=lambda e: (-e[0], e[1]))
        total = len(entries)
        shown = [rel for _, rel in entries[:_MAX_GLOB_MATCHES]]

        if not shown:
            output = "No files found (hidden and gitignored files are not listed)"
        else:
            output = "\n".join(shown)
            if total > len(shown):
                output += (
                    f"\n(Results truncated: showing {len(shown)} of {total} "
                    "matches, newest first. Narrow the pattern.)"
                )
        if ran.stdout_truncated:
            output += (
                "\n(rg output exceeded the capture cap — the listing is "
                "partial; narrow the pattern or path.)"
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

    The search IS ripgrep — ``pattern`` is an rg regex (linear-time; no
    lookaround or backreferences) and the walk carries rg's defaults:
    gitignore-aware, hidden and binary skipped, symlinks not followed. The
    parameter surface is the reference agent's, verbatim: output modes,
    ``-i``, ``-n`` (content mode, default ON), ``context`` with ``-C`` as
    its alias plus ``-A`` / ``-B``, ``-o`` (only-matching), ``type`` (rg's
    own file-type names), ``head_limit`` (``head -N``; default 250, 0 =
    unlimited) and ``offset``. ``-u`` (``--no-ignore --hidden``) is this
    tool's one extension beyond that surface. rg's stream is consumed as
    ``--json`` events and re-rendered so caps and relative paths stay under
    this tool's control.
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
                "type": {"type": "string"},
                "output_mode": {
                    "type": "string",
                    "enum": list(_GREP_OUTPUT_MODES),
                },
                "-i": {"type": "boolean"},
                "-n": {"type": "boolean"},
                "-o": {"type": "boolean"},
                "-u": {"type": "boolean"},
                "-A": {"type": "integer", "minimum": 0},
                "-B": {"type": "integer", "minimum": 0},
                "-C": {"type": "integer", "minimum": 0},
                "context": {"type": "integer", "minimum": 0},
                "head_limit": {"type": "integer", "minimum": 0},
                "offset": {"type": "integer", "minimum": 0},
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
        # The reference surface shows line numbers in content mode unless
        # explicitly switched off.
        line_numbers = bool(arguments.get("-n", True))
        only_matching = bool(arguments.get("-o")) and mode == "content"
        unrestricted = bool(arguments.get("-u"))
        multiline = bool(arguments.get("multiline"))
        after = _optional_count(arguments, "-A")
        if isinstance(after, str):
            return tool_error(self.name, after)
        before = _optional_count(arguments, "-B")
        if isinstance(before, str):
            return tool_error(self.name, before)
        around = _optional_count(arguments, "context")
        if isinstance(around, str):
            return tool_error(self.name, around)
        if around is None:  # ``-C`` is context's alias
            around = _optional_count(arguments, "-C")
            if isinstance(around, str):
                return tool_error(self.name, around)
        head_limit_raw = _optional_count(arguments, "head_limit")
        if isinstance(head_limit_raw, str):
            return tool_error(
                self.name, head_limit_raw + " (0 means unlimited)"
            )
        if head_limit_raw is None:
            head_limit: Optional[int] = _GREP_DEFAULT_HEAD_LIMIT
        elif head_limit_raw == 0:
            head_limit = None  # explicit 0 = unlimited
        else:
            head_limit = head_limit_raw
        offset = _optional_count(arguments, "offset")
        if isinstance(offset, str):
            return tool_error(self.name, offset)
        offset = offset or 0
        ctx_before = around if around is not None else (before or 0)
        ctx_after = around if around is not None else (after or 0)

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
        if glob_filter and not _looks_relative(glob_filter):
            return tool_error(
                self.name,
                "'glob' must be relative to the searched directory (no leading "
                "'/' / '..') — use 'path' to search another tree",
            )

        type_filter = arguments.get("type")
        if type_filter is not None and not isinstance(type_filter, str):
            return tool_error(self.name, "'type' must be a string")

        target_is_file = self.exec_env.is_file(resolved)
        if target_is_file or self.exec_env.is_dir(resolved):
            argv = ["rg", "--json", "--no-config", "--no-messages"]
            if not target_is_file:
                # Deterministic single-threaded walk, so a capped result keeps
                # a stable prefix across repeated searches.
                argv.append("--sort=path")
                if unrestricted:
                    argv += ["--no-ignore", "--hidden"]
                # glob / type / ignore filters scope the WALK; an explicitly
                # targeted file is always searched (the same rule that lets a
                # hidden search root be named directly).
                if glob_filter:
                    argv += ["--glob", glob_filter]
                if type_filter:
                    argv += ["--type", type_filter]
            if case_insensitive:
                argv.append("--ignore-case")
            if multiline:
                argv += ["--multiline", "--multiline-dotall"]
            if mode == "content" and not only_matching and (ctx_before or ctx_after):
                argv += [
                    "--before-context", str(ctx_before),
                    "--after-context", str(ctx_after),
                ]
            argv += ["--regexp", pattern, "--", str(resolved)]

            ran = _run_rg(self.exec_env, argv, self.workspace.root)
            if isinstance(ran, str):
                return tool_error(self.name, ran)
            # Exit 2 with something on stderr is a usage error (bad regex,
            # unknown type). Exit 2 with a SILENT stderr is per-file IO noise
            # (--no-messages suppresses only that class): rg still printed
            # every match it could reach, so the search stands — the same
            # skip-unreadable-files behaviour the walk has always had.
            reason = ran.stderr.decode("utf-8", errors="replace").strip()
            if ran.returncode not in (0, 1) and reason:
                return tool_error(self.name, f"rg: {reason}")
            partial = ran.stdout_truncated
            hits = _decode_rg_events(
                ran.stdout,
                lambda p: self.workspace.relative(Path(p)),
                only_matching=only_matching,
            )
        else:
            # A missing search root yields the empty result, not an rg usage
            # error — probing a tree that may not exist is routine.
            hits = []
            partial = False

        # A zero-match answer must not read as "nowhere in the tree" when the
        # walk filters part of it — name the escape hatch.
        empty_note = (
            ""
            if unrestricted or target_is_file
            else (
                "\n(hidden and gitignored files are not searched; pass "
                "-u: true to include them)"
            )
        )

        if mode == "files_with_matches":
            result = self._run_files_mode(hits, head_limit, offset, empty_note)
        elif mode == "count":
            result = self._run_count_mode(hits, head_limit, offset, empty_note)
        else:
            result = self._run_content_mode(
                hits,
                line_numbers=line_numbers,
                with_context=not only_matching and bool(ctx_before or ctx_after),
                head_limit=head_limit,
                offset=offset,
                empty_note=empty_note,
            )
        if partial:
            # A capture-cap hit means the decoded stream — and therefore
            # everything above — is a prefix. Say so rather than letting a
            # short list read as complete.
            result = replace(
                result,
                output=result.output
                + "\n(rg output exceeded the capture cap — results are "
                "partial; narrow the query.)",
            )
        return result

    # -- rendering helpers --------------------------------------------------

    @staticmethod
    def _window(
        items: list[Any], head_limit: Optional[int], offset: int
    ) -> list[Any]:
        """The reference surface's ``| tail -n +N | head -N`` slice:
        ``offset`` skips, then ``head_limit`` caps (``None`` = unlimited)."""
        rest = items[offset:] if offset else items
        return rest if head_limit is None else rest[:head_limit]

    def _run_files_mode(
        self,
        hits: list[_FileHits],
        head_limit: Optional[int],
        offset: int,
        empty_note: str,
    ) -> ToolResult:
        paths = [h.rel for h in hits if h.match_lines]
        shown = self._window(paths, head_limit, offset)
        if not shown:
            return ToolResult(
                success=True,
                output="No matches found" + empty_note,
                summary="grep: 0 files",
            )
        output = "\n".join(shown)
        if len(paths) > offset + len(shown):
            output += (
                f"\n(Results truncated: {len(shown)} of {len(paths)} files "
                "shown. Narrow the query or raise head_limit.)"
            )
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep: {len(shown)} file(s)",
        )

    def _run_count_mode(
        self,
        hits: list[_FileHits],
        head_limit: Optional[int],
        offset: int,
        empty_note: str,
    ) -> ToolResult:
        with_matches = [h for h in hits if h.match_lines]
        windowed = self._window(with_matches, head_limit, offset)
        rows = [f"{h.rel}:{h.match_lines}" for h in windowed]
        total = sum(h.match_lines for h in windowed)
        if not rows:
            return ToolResult(
                success=True,
                output="No matches found" + empty_note,
                summary="grep: 0 matches",
            )
        output = "\n".join(rows)
        if len(with_matches) > offset + len(rows):
            output += (
                f"\n(Results truncated: {len(rows)} of {len(with_matches)} "
                "files shown.)"
            )
        return ToolResult(
            success=True,
            output=output,
            summary=f"grep: {total} match(es) in {len(rows)} file(s)",
        )

    def _run_content_mode(
        self,
        hits: list[_FileHits],
        *,
        line_numbers: bool,
        with_context: bool,
        head_limit: Optional[int],
        offset: int,
        empty_note: str,
    ) -> ToolResult:
        # Render everything first, then slice: content-mode head_limit /
        # offset are ``head`` / ``tail`` over OUTPUT lines — context rows and
        # ``--`` separators count — exactly the reference surface's reading.
        rendered: list[tuple[str, bool]] = []  # (output line, is a match line)
        total = 0
        for h in hits:
            if not h.rows:
                continue
            total += h.match_lines
            if rendered and with_context:
                rendered.append(("--", False))
            prev_line: Optional[int] = None
            for line_no, text, is_match in h.rows:
                if (
                    with_context
                    and prev_line is not None
                    and line_no > prev_line + 1
                ):
                    rendered.append(("--", False))
                clipped = _clip_line(text)
                if line_numbers:
                    sep = ":" if is_match else "-"
                    rendered.append(
                        (f"{h.rel}{sep}{line_no}{sep}{clipped}", is_match)
                    )
                else:
                    rendered.append(
                        (f"{h.rel}{':' if is_match else '-'}{clipped}", is_match)
                    )
                prev_line = line_no
        if total == 0:
            return ToolResult(
                success=True,
                output="No matches found" + empty_note,
                summary="grep: 0 matches",
            )
        window = self._window(rendered, head_limit, offset)
        shown = sum(1 for _, is_match in window if is_match)
        out_lines = [line for line, _ in window]
        # The 32 KB inline budget is a fence, not the working rule — the
        # default head_limit keeps ordinary output far below it. Trim whole
        # lines if a wall of clipped-wide lines still overflows.
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

