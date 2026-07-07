"""Read-only fs tools: read / glob / grep.

All three tools share one ``WorkspaceRoot`` (closure-injected by
``FsToolPack``) and stay under the inline ``ToolResult`` budget via the
shared ``noeta.tools._limits`` helpers. A large ``read`` body
offloads to the ContentStore as an artifact; the model gets a bounded
excerpt + ref it can pass back to a second ``read`` with
``offset``/``limit`` to navigate the file.

Their LLM-facing descriptions are loaded from independent ``.md``
resources via ``noeta.tools.descriptions.load_tool_description``;
each carries the four-section shape (what / when / when-not / preconditions).

Path arguments are user-supplied (model-generated), so every one goes
through ``WorkspaceRoot.resolve`` — absolute / ``..`` / symlink escapes
fail before any IO. Errors degrade to ``ToolResult(success=False, ...)``
so a malformed argument or a missing file does not crash the worker.
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
from noeta.tools._invocation import (
    fit_dropping_tail,
    require_str,
    resolve_readable_file,
)
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    encoded_len,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools.descriptions import load_tool_description
from noeta.tools.fs._workspace import (
    WorkspaceEscape,
    WorkspaceRoot,
    resolve_or_error,
    tool_error,
)
from noeta.tools.fs.exec_env import ExecEnv, LocalExecEnv


__all__ = [
    "GlobTool",
    "GrepTool",
    "ReadFileTool",
]


# Read-side display caps. The model only ever sees a bounded number of
# matches inline; a longer underlying result is recorded as
# ``truncated=True`` so the model knows to narrow its query.
_MAX_GLOB_MATCHES = 200
_MAX_GREP_MATCHES = 50
_MAX_GREP_LINE_BYTES = 400
#: Max bytes of any single line fed to ``regex.search``. Nested-quantifier
#: patterns are already rejected up front; this bounds the linear-but-heavy
#: case so a single very long line can't dominate a scan.
_MAX_GREP_SCAN_LINE_BYTES = 8192
_DEFAULT_READ_LIMIT = 2000  # lines
#: Per-line visible-char cap (mirrors Claude Code's Read). A minified file can
#: be one multi-MB line; without this, a single line would dominate the inline
#: budget. The full untouched body is always the ``content_ref`` artifact.
_MAX_READ_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"
_READ_FILE_MEDIA_TYPE = "text/plain"

#: ``re`` opcodes that backtrack; POSSESSIVE_REPEAT / atomic groups do not.
_BACKTRACKING_REPEATS = frozenset(
    {_re_parser.MAX_REPEAT, _re_parser.MIN_REPEAT}
)

#: The parser's "unbounded" upper-bound sentinel (``*`` / ``+`` / ``{n,}``).
_MAXREPEAT = getattr(_re_parser, "MAXREPEAT", 4294967295)


# ---------------------------------------------------------------------------
# Image / PDF detection
#
# A ``read`` of an image the model meant to *look at* now surfaces the image
# bytes as a ``ToolResult.images`` ContentRef (T1 wired the carrying chain:
# ``wrap_tool_result_block`` → ``ToolResultBlock.images`` → adapter). read does
# NOT know whether the bound model is vision-capable (``supports_vision`` lives
# on the catalog ``ModelSpec``, consulted only inside the adapters at wire time;
# ``ToolContext`` carries only ``task_id`` / ``trace_id``), so it ALWAYS emits
# the image — the vision gate lives in the adapter, which degrades to text when
# the model can't see images.
#
# Two cases still DEGRADE to ``ToolResult(success=False, …)`` with an
# actionable message instead of emitting an image:
#   * PDFs — a document is a separate wire shape (Strategy B), out of scope here.
#   * Images over ``IMAGE_MAX_BYTES`` — no image library / auto-resize in v1, so
#     a too-large image asks the model to crop/resize first.
# A non-image binary still falls through to the generic "not utf-8" error below.
# ---------------------------------------------------------------------------

#: Leading magic bytes → a human-facing media label. Detection is by content,
#: not extension, so a mis-named or extension-less image is still caught.
#: Each entry is ``(prefix_bytes, label)``; ``webp`` needs a second check
#: (RIFF container + ``WEBP`` fourcc) handled in ``_detect_visual_media``.
_VISUAL_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
)

#: Leading magic bytes → standard image media type, for the bytes that
#: ``read`` surfaces to a vision model. Mirrors ``_VISUAL_MAGIC`` but maps to
#: a wire media type instead of a human label, and deliberately excludes PDF
#: (not an inline image). ``webp`` needs the RIFF + ``WEBP`` fourcc double-check
#: handled in ``_detect_image_media_type``.
_IMAGE_MAGIC_MEDIA_TYPE: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: Single-image inline ceiling (mirrors the chat-attachment limit). A larger
#: image degrades to text asking the model to crop/resize, rather than blow the
#: payload — v1 has no image library to auto-downscale.
IMAGE_MAX_BYTES = 5 * 1024 * 1024


def _detect_visual_media(raw: bytes) -> Optional[str]:
    """Return a media label if ``raw`` is an image (png/jpg/gif/webp) or a
    PDF, else ``None``.

    Content-sniffing by leading magic bytes — independent of the file
    extension, so a ``.png`` renamed to ``.dat`` (or vice-versa) is still
    classified correctly. ``webp`` is a RIFF container, so it needs the
    ``WEBP`` fourcc at offset 8 in addition to the ``RIFF`` prefix.
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

    ``None`` for a PDF (visual but not an inline image) or any non-image,
    so the caller routes PDFs to the degrade path. Content-sniffing by leading
    magic bytes, independent of the extension; ``webp`` needs the ``WEBP``
    fourcc at offset 8 in addition to the ``RIFF`` prefix.
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

    Walks past zero-width anchors and optional (min-zero) leading elements. A
    fixed leading ``LITERAL`` yields its charcode; anything wilder (char class /
    any / group / a repeat with a positive minimum) yields ``None``. Running out
    of tokens means the branch matches the empty string (``nullable``)."""
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
    surfaces here as a branch with an EMPTY (nullable) alternative."""
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
    entire process with no possible timeout (a separate thread can't preempt a
    GIL-holding C match). Refusing the pattern up front is the only GIL-safe
    guard. Conservative by design: it also rejects some safe shapes like
    ``(a+b)+`` / ``(foo|bar|)*`` — the model is told to flatten the pattern.
    """
    try:
        parsed = _re_parser.parse(pattern)
    except re.error:  # pragma: no cover - compile guard already reported it
        return False
    return _has_nested_repeat(parsed) or _has_overlapping_alternation_repeat(parsed)


def _clip_line(line: str) -> tuple[str, bool]:
    """Cap one line's visible chars, preserving its trailing CR/LF.

    Returns ``(possibly_clipped_line, was_clipped)``. ``splitlines(keepends=True)``
    keeps the line ending on ``line``; the marker lands before it so the line
    count and re-join stay intact.
    """
    body = line.rstrip("\r\n")
    if len(body) <= _MAX_READ_LINE_CHARS:
        return line, False
    ending = line[len(body):]
    return body[:_MAX_READ_LINE_CHARS] + _LINE_TRUNC_MARKER + ending, True


@dataclass
class ReadFileTool:
    """Read a file's contents, optionally line-sliced.

    ``offset`` is 1-based (matching common editor conventions); ``limit``
    is the number of lines to return. Both are optional; the default
    behavior reads the whole file. Any single line longer than
    ``_MAX_READ_LINE_CHARS`` is clipped (with a marker) so a minified file
    can't dominate the inline budget. When the inline ``output`` would still
    exceed ``INLINE_CONTENT_MAX_BYTES``, the full body is offloaded as a
    ContentStore artifact and the model gets
    ``{path, content_ref, excerpt, truncated}`` so it can re-read with a
    narrower slice.
    """

    workspace: WorkspaceRoot
    #: read-only directories outside the workspace this tool may
    #: also read: the skill packs (``~/.noeta/skills`` / built-in) whose
    #: absolute ``Base directory for this skill:`` line the renderer hands
    #: the model. Injected at wiring time (``build_session_inputs``) from
    #: ``resolve_skill_roots(registry)``; canonicalised (realpath). Internal
    #: config, NOT in ``input_schema`` — the tool's provider-facing schema /
    #: stable hash is unchanged, so the prompt's tool list is unaffected.
    #: Default empty ⇒ behaves exactly like the single-root wall.
    skill_roots: tuple[Path, ...] = ()
    #: execution backend for the file read (local host by default, or a
    #: sandbox container). Path *resolution* stays on ``workspace``.
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "read"
    description: str = field(default=load_tool_description("read"))
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
        read that landed under a skill root outside the workspace."""
        try:
            return self.workspace.relative(resolved)
        except ValueError:
            return resolved.as_posix()

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = require_str(
            arguments, "path", lambda m: tool_error(self.name, m),
            message="requires non-empty 'path'",
        )
        if isinstance(path, ToolResult):
            return path
        resolved = resolve_readable_file(
            self.workspace, self.skill_roots, self.name, path
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

        # an image / PDF can't be decoded as text. A supported image is
        # surfaced as a ``ToolResult.images`` ref so a vision model can see it
        # (the adapter gates on vision; read always emits — see module note).
        # A PDF or an over-limit image still degrades with a precise message
        # instead of the misleading generic "not utf-8 text" error below.
        media_label = _detect_visual_media(raw)
        if media_label is not None:
            image_media_type = _detect_image_media_type(raw)
            if image_media_type is None:
                # PDF (or any visual-but-not-inline-image): keep degrading.
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
        except UnicodeDecodeError:
            return tool_error(self.name, f"{path!r} is not utf-8 text")

        # The artifact is always the FULL file body, independent of the
        # sliced view returned inline. That way the model can pass the
        # ref through to other tools (e.g. a future source_quote-style
        # check) and the recorded artifact hash stays stable.
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
        # Hard canonical byte ceiling: if `content` does not fit, shrink it
        # to an excerpt and mark truncated. The full body is the artifact;
        # the model uses ``offset``/``limit`` to re-read or `grep` to
        # navigate.
        if encoded_len(output) > INLINE_CONTENT_MAX_BYTES:
            output["truncated"] = True
            output = fit_output_fields(
                output, shrink_order=["content"], max_bytes=INLINE_CONTENT_MAX_BYTES
            )
        summary_path = truncate_bytes(rel, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            artifacts=[ref],
            summary=(
                f"read {summary_path} "
                f"(lines {output['offset']}–{output['offset'] + output['lines_read'] - 1} "
                f"of {total_lines})"
                if output["lines_read"] > 0
                else f"read {summary_path} (empty)"
            ),
        )


def _looks_relative(pattern: str) -> bool:
    """Patterns must be workspace-relative; anchor / / abs / .. is rejected.

    ``Path.glob`` itself supports absolute patterns on POSIX, but a coding
    agent's pattern is meant to be scoped to the workspace.
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
    """Match a workspace-relative glob pattern (``Path.glob`` semantics).

    Results are workspace-relative POSIX strings, sorted for
    determinism, capped to ``_MAX_GLOB_MATCHES``. ``**`` is supported.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "glob"
    description: str = field(default=load_tool_description("glob"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
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
                self.name, "pattern must be workspace-relative (no leading '/' / '..')"
            )
        try:
            raw_matches = list(self.exec_env.glob(self.workspace.root, pattern))
        except (OSError, ValueError) as exc:
            return tool_error(self.name, f"glob failed: {exc}")

        relpaths: list[str] = []
        for match in raw_matches:
            # Containment safety: the workspace may contain symlinks; a
            # glob result whose realpath escapes is dropped.
            try:
                resolved = self.workspace.resolve(
                    os.path.relpath(match, self.workspace.root)
                )
            except WorkspaceEscape:
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

    ``pattern`` is a Python ``re`` regex. Optional ``path`` scopes to a
    file or directory (default: whole workspace). Optional ``glob``
    filters which files are searched (default: every regular file). Each
    match is ``{path, line_number, line}``; long lines are truncated for
    inline display and the count is capped at ``_MAX_GREP_MATCHES``.
    Binary files are skipped silently.
    """

    workspace: WorkspaceRoot
    exec_env: ExecEnv = field(default_factory=LocalExecEnv)
    name: str = "grep"
    description: str = field(default=load_tool_description("grep"))
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
        resolved = resolve_or_error(self.workspace, self.name, path_arg)
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
                if self.exec_env.is_file(entry) and not self.exec_env.is_symlink(entry):
                    # Containment guard against any glob/rglob result that
                    # is a symlink to outside (skipped) — non-symlinks
                    # under a contained root are already safe.
                    self.workspace.resolve(
                        os.path.relpath(entry, self.workspace.root)
                    )
                    files.append(entry)
            except (OSError, WorkspaceEscape):
                continue
        # Deterministic order: sorted by workspace-relative POSIX path.
        files.sort(key=lambda p: self.workspace.relative(p))
        return files
