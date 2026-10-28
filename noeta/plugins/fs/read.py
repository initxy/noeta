"""``Read``: a file's lines in ``cat -n`` form, or an image, PDF or notebook.

A read is observation, not mutation, so paths are not fenced: a relative path
is anchored on the workspace, an absolute one is read where it points. Every
failure is an error result, never an exception. Each text or notebook read is
recorded (``fs`` / ``read``: canonical path, sha256 of the whole file, mtime,
and the line range) for ``Edit`` / ``Write``'s read-first check and for the
unchanged-file answer: reading the same range of a file whose mtime has not
moved since, with that earlier result still in the conversation, returns a
short note instead of the content again (Claude Code's dedupe).
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import posixpath
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Image, Text
from noeta.plugins.fs._read_formats import (
    MAX_READ_TOKENS,
    read_notebook,
    read_pdf,
    read_size_tokens,
    too_many_tokens,
)
from noeta.plugins.fs.state import FS, KIND_READ, current_fs_state
from noeta.toolkit.images import ImageTooLarge, fit_image
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    Workspace,
    format_size,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["FILE_UNCHANGED", "IMAGE_MAX_BYTES", "ReadTool"]

#: The answer to a repeated read of an unchanged range.
FILE_UNCHANGED = (
    "File unchanged since you last read this range: the earlier Read result in this "
    "conversation is still current, so use it rather than reading the file again.")
#: ``allow_large`` raises the token ceiling this many times (to 200K tokens).
_LARGE_FACTOR = 8
_JS_MAX_SAFE = 9007199254740991


_DEFAULT_READ_LIMIT = 2000  # lines
#: Per-line visible-char cap: a minified file can be one multi-MB line.
_MAX_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"

#: Claude Code's whole-file ceiling for a read without ``limit`` (256 KB).
_READ_MAX_FILE_BYTES = 262_144
#: Bound on the text one scan keeps — four times the token ceiling's
#: characters, so a window the scan stops is always one the token check
#: refuses, and memory stays flat however long the requested range.
_READ_MAX_SERVED_BYTES = MAX_READ_TOKENS * 4 * 4
_READ_CHUNK_BYTES = 1024 * 1024
_SNIFF_BYTES = 16
#: Bytes of one line kept while streaming: enough for ``_MAX_LINE_CHARS``
#: characters of up to four UTF-8 bytes each, plus a trailing ``\r``.
_LINE_KEEP_BYTES = _MAX_LINE_CHARS * 4 + 1

#: Largest image file read at all (Claude Code's); what is read is then made
#: to fit a request by :func:`noeta.toolkit.images.fit_image`.
IMAGE_MAX_BYTES = 128 * 1024 * 1024

#: Leading magic bytes -> (human label, media type). Detection is by content,
#: not extension, so a mis-named image is still caught. WebP (a RIFF
#: container with a ``WEBP`` fourcc at offset 8) is checked separately.
_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image", "image/png"),
    (b"\xff\xd8\xff", "JPEG image", "image/jpeg"),
    (b"GIF87a", "GIF image", "image/gif"),
    (b"GIF89a", "GIF image", "image/gif"),
)


def detect_image(raw: bytes) -> tuple[str, str] | None:
    """``(label, media type)`` when ``raw`` starts like a png / jpeg / gif /
    webp image, else ``None``."""
    for prefix, label, media_type in _IMAGE_MAGIC:
        if raw.startswith(prefix):
            return label, media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "WebP image", "image/webp"
    return None


def _note(text: str) -> str:
    """A tool's own inline note, parenthesised. Deliberately not a
    ``<system-reminder>``: that tag belongs to the host."""
    return f"({text})"


def _decode_line(raw: bytes, clipped: bool) -> tuple[str, bool]:
    """One line's display text (a trailing ``\\r`` dropped, a long line
    clipped) and whether invalid UTF-8 was replaced. A clipped line may end
    mid-character; the incremental decoder holds that partial back."""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        text = codecs.getincrementaldecoder("utf-8")("strict").decode(
            raw, final=not clipped
        )
        replaced = False
    except UnicodeDecodeError:
        text = codecs.getincrementaldecoder("utf-8")("replace").decode(
            raw, final=not clipped
        )
        replaced = True
    if clipped or len(text) > _MAX_LINE_CHARS:
        return text[:_MAX_LINE_CHARS] + _LINE_TRUNC_MARKER, replaced
    return text, replaced


@dataclass
class _Scan:
    """One streaming pass over a file: hash every byte, count lines, keep only
    the lines ``[start, start + limit)`` that fit the served-bytes bound.

    Lines split on ``\\n`` only — the boundary ``rg`` uses — so a
    form feed or U+2028 inside a line never shifts the numbering. A trailing
    segment without ``\\n`` is the last line. A NUL byte stops the scan (the
    file is binary)."""

    start: int
    limit: int
    max_served: int = _READ_MAX_SERVED_BYTES
    hasher: Any = field(default_factory=hashlib.sha256)
    window: list[str] = field(default_factory=list)
    served: int = 0
    capped: bool = False
    bytes_replaced: bool = False
    has_nul: bool = False
    #: The file's last byte is a newline (Read then shows a final empty line).
    ends_with_newline: bool = False
    total_lines: int = 0  # completed lines so far; final after ``finish``
    _cur: bytearray = field(default_factory=bytearray)
    _cur_len: int = 0

    @property
    def digest(self) -> str:
        return str(self.hasher.hexdigest())

    def window_text(self) -> str:
        return "\n".join(self.window)

    def _take(self, line_no: int) -> bool:
        return self.start <= line_no < self.start + self.limit and not self.capped

    def _finish_line(self, raw: bytes, clipped: bool) -> None:
        text, replaced = _decode_line(raw, clipped)
        cost = len(text.encode("utf-8")) + 8  # number column + tab + newline
        if self.window and self.served + cost > self.max_served:
            self.capped = True
            return
        self.window.append(text)
        self.served += cost
        self.bytes_replaced = self.bytes_replaced or replaced

    def feed(self, chunk: bytes) -> bool:
        """Take one chunk; ``False`` once the file proved binary."""
        if b"\x00" in chunk:
            self.has_nul = True
            return False
        self.hasher.update(chunk)
        if chunk:
            self.ends_with_newline = chunk.endswith(b"\n")
        pos, n = 0, len(chunk)
        while pos < n:
            nl = chunk.find(b"\n", pos)
            stop = n if nl < 0 else nl
            if self._take(self.total_lines) and len(self._cur) < _LINE_KEEP_BYTES:
                self._cur += chunk[pos : min(stop, pos + _LINE_KEEP_BYTES - len(self._cur))]
            self._cur_len += stop - pos
            if nl < 0:
                break
            if self._take(self.total_lines):
                self._finish_line(bytes(self._cur), self._cur_len > _LINE_KEEP_BYTES)
            self.total_lines += 1
            self._cur.clear()
            self._cur_len = 0
            pos = nl + 1
        return True

    def finish(self) -> None:
        if self._cur_len:
            if self._take(self.total_lines):
                self._finish_line(bytes(self._cur), self._cur_len > _LINE_KEEP_BYTES)
            self.total_lines += 1
            self._cur.clear()
            self._cur_len = 0


def _chunks(env: ExecEnv, path: str) -> Callable[[], AsyncIterator[bytes]]:
    """A factory of bounded-chunk iterators over ``path``, so memory stays
    flat however large the file is."""

    async def _iter() -> AsyncIterator[bytes]:
        offset = 0
        while True:
            chunk = await env.read_range(path, offset, _READ_CHUNK_BYTES)
            if chunk:
                yield chunk
            if len(chunk) < _READ_CHUNK_BYTES:
                return
            offset += len(chunk)

    return _iter


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _input_schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "file_path": {"type": "string",
                          "description": "Absolute path of the file to read."},
            "offset": {"type": "integer", "minimum": 0, "maximum": _JS_MAX_SAFE,
                       "description": ("Line number to start from (the first line is 1). "
                                       "Give it only when the file is too large to read "
                                       "at once.")},
            "limit": {"type": "integer", "exclusiveMinimum": 0, "maximum": _JS_MAX_SAFE,
                      "description": ("How many lines to read. Give it only when the file "
                                      "is too large to read at once.")},
            "pages": {"type": "string",
                      "description": ('Pages of a PDF to read, such as "1-5", "3" or "10-20". '
                                      "PDF files only; at most 20 pages per call.")},
            "allow_large": {"type": "boolean",
                            "description": (
                                "Read a file or line range past the usual size limits, up to "
                                "what fits in your context. Only when you need all of it or "
                                "the user asked for the whole file; otherwise read it in "
                                "parts or search it.")},
        },
        "required": ["file_path"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class _Window:
    """The file one text read serves and the lines asked for."""

    path: str
    resolved: str
    display: str
    size: int
    mtime: float
    offset: int
    limit: int
    explicit_limit: int | None
    large: bool


class ReadTool:
    """``Read``. ``offset`` is 1-based."""

    name = "Read"
    description = load_description("noeta.plugins.fs", "read")
    read_only = True
    clearable = True
    input_schema: dict[str, Any]

    def __init__(self) -> None:
        self.input_schema = _input_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "file_path")
        if path is None:
            return tool_error(self.name, "requires non-empty 'file_path'")
        env = ctx.env
        ws = await Workspace.of(env)
        resolved = await ws.canonical(path)
        st = await env.stat(resolved)
        if st is None or st.is_dir:
            return tool_error(self.name, f"not a file: {path!r}")
        display = ws.relative(resolved)
        offset = _positive_int(input.get("offset")) or 1
        explicit_limit = _positive_int(input.get("limit"))
        limit = explicit_limit or _DEFAULT_READ_LIMIT
        large = input.get("allow_large") is True
        seen = current_fs_state(ctx.state).reads.get(resolved)
        if ctx.by_model and seen is not None and seen.range == [offset, limit] \
                and seen.mtime == st.mtime and not input.get("pages"):
            return text_output(FILE_UNCHANGED, path=display, unchanged=True)
        size = st.size
        chunks = _chunks(env, resolved)

        try:
            head = await env.read_range(resolved, 0, _SNIFF_BYTES)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")

        if head.startswith(b"%PDF-"):
            return await read_pdf(self.name, env, resolved, display, size, input.get("pages"))

        if posixpath.splitext(resolved)[1] == ".ipynb":
            try:
                raw_nb = b"".join([c async for c in chunks()])
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            _record_read(ctx, resolved, hashlib.sha256(raw_nb).hexdigest(), st.mtime)
            return read_notebook(self.name, raw_nb, display)

        image = detect_image(head)
        if image is not None:
            return await self._read_image(path, resolved, display, size, image, chunks)

        return await self._read_text(
            ctx, _Window(path=path, resolved=resolved, display=display, size=size,
                         mtime=st.mtime, offset=offset, limit=limit,
                         explicit_limit=explicit_limit, large=large), chunks)

    async def _read_text(self, ctx: ToolContext, w: _Window,
                         chunks: Callable[[], AsyncIterator[bytes]]) -> ToolOutput:
        """A text file's window in ``cat -n`` form, within Claude Code's
        size and token ceilings; the read is recorded."""
        # Claude Code's whole-file ceiling: a read with no ``limit`` of a file
        # over 256 KB is refused before anything is served.
        if w.explicit_limit is None and w.size > _READ_MAX_FILE_BYTES and not w.large:
            return tool_error(
                self.name,
                f"File content ({format_size(w.size)}) exceeds maximum allowed "
                f"size ({format_size(_READ_MAX_FILE_BYTES)}). Use offset and "
                "limit parameters to read specific portions of the file, or "
                "search for specific content instead of reading the whole "
                "file.",
            )

        start = w.offset - 1
        max_tokens = MAX_READ_TOKENS * (_LARGE_FACTOR if w.large else 1)
        scan = _Scan(start=start, limit=w.limit,
                     max_served=_READ_MAX_SERVED_BYTES * (_LARGE_FACTOR if w.large else 1))
        try:
            async for chunk in chunks():
                if not scan.feed(chunk):
                    break
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        # A NUL byte marks real binary regardless of decodability.
        if scan.has_nul:
            return tool_error(self.name, f"{w.path!r} is not utf-8 text")
        scan.finish()
        # Claude Code's token ceiling on what one read returns. A window the
        # scan had to stop is past it by construction.
        text = scan.window_text()
        tokens = read_size_tokens(text, posixpath.splitext(w.resolved)[1].lstrip(".").lower())
        if not scan.capped and tokens > max_tokens // 4 and ctx.count_tokens is not None:
            # Past a quarter of the ceiling the estimate is checked against
            # the provider's exact count, as Claude Code does.
            tokens = await ctx.count_tokens(text) or tokens
        if scan.capped or tokens > max_tokens:
            return tool_error(self.name, too_many_tokens(tokens, max_tokens))

        # Keyed by the canonical path and digesting the raw bytes of the whole
        # file, so a later mutation check compares like with like.
        _record_read(ctx, w.resolved, scan.digest, w.mtime, [w.offset, w.limit])

        return _render(scan, w)

    async def _read_image(
        self,
        path: str,
        resolved: str,
        display: str,
        size: int,
        image: tuple[str, str],
        chunks: Callable[[], AsyncIterator[bytes]],
    ) -> ToolOutput:
        label, media_type = image
        too_big = (
            f"over the {IMAGE_MAX_BYTES // 1024 // 1024}MB image read limit — "
            "resize it or export a smaller copy, then read that"
        )
        if size > IMAGE_MAX_BYTES:
            return tool_error(self.name, f"{path!r} is a {label} of {size} bytes, {too_big}")
        buf = bytearray()
        try:
            async for chunk in chunks():
                buf += chunk
                if len(buf) > IMAGE_MAX_BYTES:  # grew while being read
                    return tool_error(
                        self.name,
                        f"{path!r} is a {label} of more than "
                        f"{IMAGE_MAX_BYTES} bytes, {too_big}",
                    )
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        try:
            fitted = await asyncio.to_thread(fit_image, bytes(buf), media_type)
        except ImageTooLarge as exc:
            return tool_error(self.name, f"{path!r} is a {label} that cannot be sent: {exc}")
        text = f"Read image {display} ({fitted.media_type}, {len(fitted.data)} bytes)"
        note = fitted.note()
        return ToolOutput(
            content=[
                Text(text=f"{text}\n{note}" if note else text),
                Image(media_type=fitted.media_type, data=fitted.data),
            ],
            meta={"path": display, "media_type": fitted.media_type,
                  "bytes": len(fitted.data)},
        )


def _render(scan: _Scan, w: _Window) -> ToolOutput:
    """The scanned window as numbered lines, or a note when it holds none."""
    start = w.offset - 1
    total = scan.total_lines
    meta: dict[str, Any] = {"path": w.display, "total_lines": total}
    if scan.bytes_replaced:
        meta["bytes_replaced"] = True
    if total == 0:
        return text_output(_note("The file exists but has empty contents."), **meta)
    if start >= total:
        return text_output(
            _note(
                f"The file has only {total} lines, fewer than the "
                f"requested offset {w.offset}. Nothing was read."
            ),
            **meta,
        )

    lines = list(scan.window)
    # A final newline shows as one more, empty, numbered line.
    if scan.ends_with_newline and start + len(lines) == total and len(lines) < w.limit \
            and not scan.capped:
        lines.append("")
    rendered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(lines))
    shown_end = start + len(scan.window)
    if scan.bytes_replaced:
        rendered += "\n\n" + _note("Non-utf8 bytes were replaced with U+FFFD.")
    return text_output(rendered, lines=[start + 1, shown_end], **meta)


def _record_read(ctx: ToolContext, path: str, digest: str, mtime: float,
                 lines: list[int] | None = None) -> None:
    data: dict[str, Any] = {"path": path, "digest": digest, "mtime": mtime}
    if lines is not None:
        data["range"] = lines
    ctx.record(FS, KIND_READ, data)
