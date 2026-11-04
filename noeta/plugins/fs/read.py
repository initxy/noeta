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
import math
import posixpath
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.images import ImageTooLarge, fit_image
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Image
from noeta.plugins.fs._encoding import (
    NUL_PROBE_BYTES,
    UTF16BE_BOM,
    NotText,
    encoding_of,
    read_note,
)
from noeta.plugins.fs._missing import missing_file_message
from noeta.plugins.fs._read_formats import (
    MAX_READ_TOKENS,
    NOTEBOOK_MAX_BYTES,
    notebook_too_large,
    read_notebook,
    read_pdf,
    read_size_tokens,
    too_many_tokens,
)
from noeta.plugins.fs.state import FS, KIND_NOTICE, KIND_READ, current_fs_state
from noeta.toolkit.plugin_records import shown_instruction_files
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    Workspace,
    format_size,
    input_error,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["BINARY_EXTENSIONS", "FILE_UNCHANGED", "IMAGE_MAX_BYTES", "ReadTool"]

#: The answer to a repeated read of an unchanged range.
FILE_UNCHANGED = (
    "Wasted call: this range of the file has not changed since your last Read of it. Use "
    "that earlier result instead of reading again.")
#: The answer to a whole-file read of an instruction file shown at the start
#: (or brought by a touched file) and unchanged on disk since (Claude Code's
#: ``file_unchanged`` for a seeded file).
SHOWN_UNCHANGED = (
    "This file's text is already in your context, in the instruction files shown above, and "
    "the file has not changed on disk since. Use that text rather than reading the file "
    "again.")
#: ``allow_large`` raises the token ceiling this many times (to 200K tokens).
_LARGE_FACTOR = 8
_JS_MAX_SAFE = 9007199254740991

#: Without ``limit`` every line is read (Claude Code's reader sets no line
#: count of its own); the size and token ceilings bound the window instead.
_ALL_LINES = _JS_MAX_SAFE

#: Claude Code's whole-file ceiling for a read without ``limit`` (256 KB).
_READ_MAX_FILE_BYTES = 262_144
#: Bound on the text one scan keeps — four times the token ceiling's
#: characters, so a window the scan stops is always one the token check
#: refuses, and memory stays flat however long the requested range.
_READ_MAX_SERVED_BYTES = MAX_READ_TOKENS * 4 * 4
_READ_CHUNK_BYTES = 1024 * 1024
_SNIFF_BYTES = 16

#: Largest image file read at all (Claude Code's); what is read is then made
#: to fit a request by :func:`noeta.core.images.fit_image`.
IMAGE_MAX_BYTES = 128 * 1024 * 1024

#: Extensions read as images (Claude Code's); a file without an extension is
#: an image when its content says so.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

#: Leading magic bytes -> (human label, media type), which confirm an image
#: extension and name the format. WebP (a RIFF container with a ``WEBP``
#: fourcc at offset 8) is checked separately.
_IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image", "image/png"),
    (b"\xff\xd8\xff", "JPEG image", "image/jpeg"),
    (b"GIF87a", "GIF image", "image/gif"),
    (b"GIF89a", "GIF image", "image/gif"),
)


#: Extensions Read refuses unopened (Claude Code's list), less the image and
#: PDF kinds it shows.
BINARY_EXTENSIONS = frozenset({
    ".bmp", ".ico", ".tiff", ".tif", ".heic", ".heif", ".avif",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso", ".zst", ".lz4",
    ".whl", ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib", ".app",
    ".msi", ".deb", ".rpm", ".dmg", ".apk",
    ".doc", ".docx", ".docm", ".dotx", ".dotm", ".xls", ".xlsx", ".xlsm", ".xlsb", ".xltx",
    ".xltm", ".ppt", ".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".odt", ".ods",
    ".odp", ".numbers", ".epub", ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx", ".parquet", ".feather", ".npy", ".npz",
    ".pickle", ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    ".swf", ".fla", ".lockb", ".dat", ".data",
})


#: Device and process files Read refuses (Claude Code's): they never end, block
#: on input, or hand out another process's secrets.
_DEVICE_FILES = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full", "/dev/stdin", "/dev/tty",
    "/dev/console", "/proc/kmsg", "/dev/stdout", "/dev/stderr", "/dev/fd/0", "/dev/fd/1",
    "/dev/fd/2",
})
_DEVICE_PATTERN = re.compile(
    r"^/dev/(?:pts/(?:\d+|ptmx)|ptmx|tty[^/]*)$"
    r"|^/proc/[^/]+/fd/[012]$"
    r"|^/proc/[^/]+/(?:environ|cmdline|auxv|maps|smaps|smaps_rollup|numa_maps|pagemap|mem"
    r"|stat|statm|status|syscall|stack)$")


def is_device_file(path: str) -> bool:
    return path in _DEVICE_FILES or _DEVICE_PATTERN.match(path) is not None


def detect_image(raw: bytes) -> tuple[str, str] | None:
    """``(label, media type)`` when ``raw`` starts like a png / jpeg / gif /
    webp image, else ``None``."""
    for prefix, label, media_type in _IMAGE_MAGIC:
        if raw.startswith(prefix):
            return label, media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "WebP image", "image/webp"
    return None


def _decode_line(raw: bytes) -> tuple[str, bool]:
    """One line's text (a trailing ``\\r`` dropped) and whether invalid
    UTF-8 was replaced. A line is never cut short (Claude Code's)."""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), True


@dataclass
class _Scan:
    """One streaming pass over a file: hash every byte, count lines, keep only
    the lines ``[start, start + limit)`` that fit the served-bytes bound.

    Lines split on ``\\n`` only — the boundary ``rg`` uses — so a
    form feed or U+2028 inside a line never shifts the numbering. A trailing
    segment without ``\\n`` is the last line. ``head`` (the file's first
    bytes) picks the encoding (:mod:`noeta.plugins.fs._encoding`): a UTF-16LE
    file is turned into UTF-8 before it is split; the hash is of the bytes on
    disk."""

    start: int
    limit: int
    head: bytes = b""
    max_served: int = _READ_MAX_SERVED_BYTES
    hasher: Any = field(default_factory=hashlib.sha256)
    window: list[str] = field(default_factory=list)
    served: int = 0
    capped: bool = False
    bytes_replaced: bool = False
    #: A NUL byte within the first ``NUL_PROBE_BYTES`` bytes.
    early_nul: bool = False
    #: Every byte so far decodes as UTF-8.
    valid_utf8: bool = True
    #: Every byte so far decodes as UTF-16LE (a file behind its mark).
    valid_utf16: bool = True
    _seen: int = 0
    _utf16: Any = None
    _utf16_strict: Any = None
    _utf8: Any = None
    #: The file's last byte is a newline (Read then shows a final empty line).
    ends_with_newline: bool = False
    total_lines: int = 0  # completed lines so far; final after ``finish``
    _cur: bytearray = field(default_factory=bytearray)
    _cur_len: int = 0

    def __post_init__(self) -> None:
        if encoding_of(self.head) == "utf-16-le":
            self._utf16 = codecs.getincrementaldecoder("utf-16-le")("replace")
            self._utf16_strict = codecs.getincrementaldecoder("utf-16-le")("strict")
        else:
            self._utf8 = codecs.getincrementaldecoder("utf-8")("strict")

    @property
    def digest(self) -> str:
        return str(self.hasher.hexdigest())

    def not_text(self) -> NotText | None:
        """Why the file is not text (after ``finish``), as
        :func:`noeta.plugins.fs._encoding.not_text` would say."""
        if self._utf16 is not None:
            return None if self.valid_utf16 else "not_utf16"
        if self.head.startswith(UTF16BE_BOM):
            return "utf16be"
        if self.early_nul:
            return "nul"
        return None if self.valid_utf8 else "not_utf8"

    def _check_utf16(self, chunk: bytes, final: bool = False) -> None:
        if self._utf16_strict is None or not self.valid_utf16:
            return
        try:
            self._utf16_strict.decode(chunk, final)
        except UnicodeDecodeError:
            self.valid_utf16 = False

    def _check_utf8(self, chunk: bytes, final: bool = False) -> None:
        if self._utf8 is None or not self.valid_utf8:
            return
        try:
            self._utf8.decode(chunk, final)
        except UnicodeDecodeError:
            self.valid_utf8 = False

    def window_text(self) -> str:
        return "\n".join(self.window)

    def _take(self, line_no: int) -> bool:
        return self.start <= line_no < self.start + self.limit and not self.capped

    def _finish_line(self, raw: bytes, over: bool) -> None:
        if over:  # one line past the bound: the window cannot be served
            self.capped = True
            return
        text, replaced = _decode_line(raw)
        if self.total_lines == 0 and text.startswith("\ufeff"):
            text = text[1:]  # a byte-order mark is not content (Claude Code's)
        cost = len(text.encode("utf-8")) + 8  # number column + tab + newline
        if self.window and self.served + cost > self.max_served:
            self.capped = True
            return
        self.window.append(text)
        self.served += cost
        self.bytes_replaced = self.bytes_replaced or replaced

    def feed(self, chunk: bytes) -> None:
        """Take the file's next chunk."""
        self.hasher.update(chunk)
        if self._seen < NUL_PROBE_BYTES and b"\0" in chunk[:NUL_PROBE_BYTES - self._seen]:
            self.early_nul = True
        self._seen += len(chunk)
        if self._utf16 is not None:
            self._check_utf16(chunk)
            chunk = self._utf16.decode(chunk).encode("utf-8")
        self._check_utf8(chunk)
        self._split(chunk)

    def _split(self, chunk: bytes) -> None:
        if chunk:
            self.ends_with_newline = chunk.endswith(b"\n")
        pos, n = 0, len(chunk)
        while pos < n:
            nl = chunk.find(b"\n", pos)
            stop = n if nl < 0 else nl
            if self._take(self.total_lines) and len(self._cur) <= self.max_served:
                self._cur += chunk[pos : min(stop, pos + self.max_served + 1 - len(self._cur))]
            self._cur_len += stop - pos
            if nl < 0:
                break
            if self._take(self.total_lines):
                self._finish_line(bytes(self._cur), self._cur_len > self.max_served)
            self.total_lines += 1
            self._cur.clear()
            self._cur_len = 0
            pos = nl + 1

    def finish(self) -> None:
        if self._utf16 is not None:
            self._check_utf16(b"", True)
            self._split(self._utf16.decode(b"", True).encode("utf-8"))
        self._check_utf8(b"", True)
        if self._cur_len:
            if self._take(self.total_lines):
                self._finish_line(bytes(self._cur), self._cur_len > self.max_served)
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
                                "what fits in your context. Use it only when every line is "
                                "needed or the whole file was asked for by the user; "
                                "otherwise read it in pieces or search it.")},
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
    #: The file's first bytes, which pick its encoding.
    head: bytes = b""


class ReadTool:
    """``Read``. ``offset`` is 1-based."""

    name = "Read"
    description = load_description("noeta.plugins.fs", "read")
    read_only = True
    retry_safe = True
    clearable = True
    #: Bounded by its own size and token ceilings, as Claude Code's Read.
    max_result_chars = None
    input_schema: dict[str, Any]

    def __init__(self) -> None:
        self.input_schema = _input_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "file_path")
        if path is None:
            return input_error(f"{self.name} requires non-empty 'file_path'")
        env = ctx.env
        ws = await Workspace.of(env)
        given = posixpath.normpath(posixpath.join(ws.root, path))
        if is_device_file(given):
            return input_error(f"Read does not read device or process files, and {path} is one.")
        resolved = await ws.canonical(path)
        if is_device_file(resolved):
            return input_error(f"Read does not read device or process files, and {path} is one.")
        ext = posixpath.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return input_error(
                f"Read does not open binary files, and a {ext} file is one. Open it with a "
                "skill made for the format, or with a shell command or script that "
                "understands it.")
        st = await env.stat(resolved)
        if st is None:
            return input_error(await missing_file_message(ws, resolved))
        if st.is_dir:
            return input_error(f"not a file: {path!r}")
        display = ws.relative(resolved)
        offset = _positive_int(input.get("offset")) or 1
        explicit_limit = _positive_int(input.get("limit"))
        limit = explicit_limit or _ALL_LINES
        large = input.get("allow_large") is True
        seen = current_fs_state(ctx.state).reads.get(resolved)
        asked = [offset, explicit_limit or 0]
        if ctx.by_model and seen is not None and not seen.partial and seen.range == asked \
                and seen.mtime == st.mtime and not input.get("pages"):
            return text_output(FILE_UNCHANGED, path=display, unchanged=True)
        if ctx.by_model and seen is None and offset == 1 and explicit_limit is None \
                and not input.get("pages") and _shown_unchanged(ctx.state, resolved, st.mtime):
            return text_output(SHOWN_UNCHANGED, path=display, unchanged=True)
        size = st.size
        chunks = _chunks(env, resolved)

        try:
            head = await env.read_range(resolved, 0, _SNIFF_BYTES)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")

        if ext == ".pdf" or (ext == "" and head.startswith(b"%PDF-")):
            return await read_pdf(self.name, env, resolved, display, size, input.get("pages"))

        if posixpath.splitext(resolved)[1] == ".ipynb":
            if size > NOTEBOOK_MAX_BYTES:
                return notebook_too_large(self.name)
            try:
                raw_nb = b"".join([c async for c in chunks()])
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            notebook = read_notebook(self.name, raw_nb, display)
            if not notebook.is_error:
                _record_read(ctx, resolved, hashlib.sha256(raw_nb).hexdigest(), st.mtime,
                             asked, whole=offset <= 1 and explicit_limit is None)
            return notebook

        image = detect_image(head)
        ext = posixpath.splitext(resolved)[1].lower()
        if ext in _IMAGE_EXTENSIONS and image is None:
            return input_error(
                f"{display} has an image extension, but its bytes are not a PNG, JPEG, GIF or "
                "WebP image (often a saved error or login page). Check it with `file`, or "
                "look at it as text through Bash (`head -c 500`).")
        if image is not None and (ext in _IMAGE_EXTENSIONS or ext == ""):
            return await self._read_image(ctx, path, display, size, image, chunks)

        return await self._read_text(
            ctx, _Window(path=path, resolved=resolved, display=display, size=size,
                         mtime=st.mtime, offset=offset, limit=limit,
                         explicit_limit=explicit_limit, large=large, head=head), chunks)

    async def _read_text(self, ctx: ToolContext, w: _Window,
                         chunks: Callable[[], AsyncIterator[bytes]]) -> ToolOutput:
        """A text file's window in ``cat -n`` form, within Claude Code's
        size and token ceilings; the read is recorded."""
        # Claude Code's whole-file ceiling: a read with no ``limit`` of a file
        # over 256 KB is refused before anything is served.
        if w.explicit_limit is None and w.size > _READ_MAX_FILE_BYTES and not w.large:
            return tool_error(
                self.name,
                f"At {format_size(w.size)} the file is over the "
                f"{format_size(_READ_MAX_FILE_BYTES)} limit for a read of the whole "
                "file. Read a part with offset and limit, or search the file for "
                "what you need.",
                too_large=True,
            )

        start = w.offset - 1
        max_tokens = MAX_READ_TOKENS * (_LARGE_FACTOR if w.large else 1)
        scan = _Scan(start=start, limit=w.limit, head=w.head,
                     max_served=_READ_MAX_SERVED_BYTES * (_LARGE_FACTOR if w.large else 1))
        try:
            async for chunk in chunks():
                scan.feed(chunk)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        scan.finish()
        # Claude Code's token ceiling on what one read returns. A window the
        # scan had to stop is past it by construction.
        text = scan.window_text()
        tokens = read_size_tokens(text, posixpath.splitext(w.resolved)[1].lstrip(".").lower())
        if not scan.capped and tokens > max_tokens // 4 and ctx.count_tokens is not None:
            # Past a quarter of the ceiling the estimate is checked against
            # the provider's exact count, as Claude Code does.
            tokens = await ctx.count_tokens(text) or tokens
        if not scan.capped and tokens > max_tokens and w.offset <= 1 and w.explicit_limit is None:
            return self._first_page(ctx, w, scan, text, tokens, max_tokens)
        if scan.capped or tokens > max_tokens:
            return tool_error(self.name, too_many_tokens(tokens, max_tokens), too_large=True)

        # Keyed by the canonical path and digesting the raw bytes of the whole
        # file, so a later mutation check compares like with like.
        whole = w.offset <= 1 and (w.explicit_limit is None
                                   or len(scan.window) < w.explicit_limit)
        _record_read(ctx, w.resolved, scan.digest, w.mtime, [w.offset, w.explicit_limit or 0],
                     whole=whole)

        return _render(scan, w)

    def _first_page(self, ctx: ToolContext, w: _Window, scan: _Scan, text: str,
                    tokens: int, cap: int) -> ToolOutput:
        """A whole-file read past the token ceiling: Claude Code returns the
        first part that fits and tells the model, after the result, how to
        page on. The read counts as partial (not read, for a later write)."""
        page, by_chars = _first_part(text, tokens, cap)
        shown = page.count("\n") + 1
        total = scan.total_lines + (1 if scan.ends_with_newline else 0)
        hint = (" Even with allow_large it does not fit." if w.large else
                " allow_large: true returns more of it; use that only when every part matters "
                "or the user wants the file entire.")
        head = (f"[Partial view, cut to fit the read limit: {w.resolved}")
        size = f"({tokens} tokens; the cap is {cap})"
        if not by_chars and shown < total:
            banner = (f"{head} shows lines 1-{shown} of {total} {size}. For the next part call "
                      f"Read with offset={shown + 1} limit={shown}, or search the file for the "
                      f"section you need.{hint} What you need may lie past "
                      "this part; check before relying on it alone.]")
        else:
            banner = (f"{head} shows its first {len(page)} of {len(text)} characters {size}; "
                      "its lines are too long to page by line. Search the file for the section "
                      f"you need, or page through it with Read's offset and limit.{hint} What you "
                      "need may lie past this excerpt; check before relying on it alone.]")
        _record_read(ctx, w.resolved, scan.digest, w.mtime, [1, shown], partial=True)
        ctx.record(FS, KIND_NOTICE, {"text": banner})
        rendered = "\n".join(f"{i}\t{line}" for i, line in enumerate(page.split("\n"), 1))
        reason = scan.not_text()
        if reason is not None:
            rendered += "\n\n" + read_note(reason)
        return text_output(rendered, path=w.display, total_lines=scan.total_lines,
                           lines=[1, shown], truncated=True)

    async def _read_image(
        self,
        ctx: ToolContext,
        path: str,
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
        # Claude Code's result is the image alone; how its pixels map back to
        # the file (when it was scaled) follows as a note.
        note = fitted.note()
        if note:
            ctx.record(FS, KIND_NOTICE, {"text": f"{display}: {note}"})
        return ToolOutput(
            content=[Image(media_type=fitted.media_type, data=fitted.data)],
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
    reason = scan.not_text()
    if reason is not None:
        meta["not_text"] = reason
    if total == 0:
        return text_output("Warning: the file is there but holds no content.", **meta)
    # Claude Code counts a final newline as starting one more, empty, line.
    lines_counted = total + (1 if scan.ends_with_newline else 0)
    if start >= lines_counted:
        return text_output(
            f"Warning: offset {w.offset} is past the end of the file, which has "
            f"{lines_counted} lines.",
            **meta,
        )

    lines = list(scan.window)
    # A final newline shows as one more, empty, numbered line.
    if scan.ends_with_newline and start + len(lines) == total and len(lines) < w.limit \
            and not scan.capped:
        lines.append("")
    rendered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(lines))
    shown_end = start + len(scan.window)
    if reason is not None:
        # Claude Code's note: why Edit and Write will refuse the file.
        rendered += "\n\n" + read_note(reason)
    return text_output(rendered, lines=[start + 1, shown_end], **meta)


def _first_part(text: str, tokens: int, cap: int) -> tuple[str, bool]:
    """The leading whole lines of ``text`` that fit ``cap`` tokens (Claude
    Code's sizing: 85% of the proportional share, shrunk until it fits), or
    its leading characters when not even one line fits; and whether it was
    cut by characters."""
    lines = text.split("\n")
    per_token = max(0.5, len(text) / max(1, tokens))

    def fits(part: str) -> bool:
        return len(part) / per_token <= cap

    count = max(1, min(len(lines), math.floor(len(lines) * cap / max(1, tokens) * 0.85)))
    page = "\n".join(lines[:count])
    for _ in range(6):
        if fits(page) or count <= 1:
            break
        count = max(1, math.floor(count * 0.7))
        page = "\n".join(lines[:count])
    if fits(page) and page.strip():
        return page, False
    size = max(1, math.floor(cap * per_token * 0.85))
    for _ in range(6):
        page = text[:size]
        if fits(page):
            break
        size = max(1, math.floor(size * 0.7))
    return page, True


def _record_read(ctx: ToolContext, path: str, digest: str, mtime: float,
                 lines: list[int] | None = None, *, partial: bool = False,
                 whole: bool = True) -> None:
    data: dict[str, Any] = {"path": path, "digest": digest, "mtime": mtime}
    if lines is not None:
        data["range"] = lines
    if partial:
        data["partial"] = True
    if not whole or partial:
        data["whole"] = False
    ctx.record(FS, KIND_READ, data)


def _shown_unchanged(states: Mapping[str, Any], resolved: str, mtime: float) -> bool:
    """Whether the instructions plugin showed the file at ``resolved`` whole
    (its state, read by name) and it still has the ``mtime`` it had then."""
    for entry in shown_instruction_files(states):
        if entry.get("real") == resolved \
                and entry.get("mtime") == mtime and entry.get("whole", True):
            return True
    return False
