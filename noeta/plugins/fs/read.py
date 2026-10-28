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
import hashlib
import math
import posixpath
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Image
from noeta.plugins.fs._missing import missing_file_message
from noeta.plugins.fs._read_formats import (
    MAX_READ_TOKENS,
    read_notebook,
    read_pdf,
    read_size_tokens,
    too_many_tokens,
)
from noeta.plugins.fs.state import FS, KIND_NOTICE, KIND_READ, current_fs_state
from noeta.toolkit.images import ImageTooLarge, fit_image
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    Workspace,
    format_size,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["BINARY_EXTENSIONS", "FILE_UNCHANGED", "IMAGE_MAX_BYTES", "ReadTool"]

#: The answer to a repeated read of an unchanged range.
FILE_UNCHANGED = (
    "File unchanged since you last read this range: the earlier Read result in this "
    "conversation is still current, so use it rather than reading the file again.")
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
#: to fit a request by :func:`noeta.toolkit.images.fit_image`.
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


def _note(text: str) -> str:
    """A tool's own inline note, parenthesised. Deliberately not a
    ``<system-reminder>``: that tag belongs to the host."""
    return f"({text})"


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

    def _finish_line(self, raw: bytes, over: bool) -> None:
        if over:  # one line past the bound: the window cannot be served
            self.capped = True
            return
        text, replaced = _decode_line(raw)
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
        return True

    def finish(self) -> None:
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
    retry_safe = True
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
        given = posixpath.normpath(posixpath.join(ws.root, path))
        if is_device_file(given):
            return tool_error(
                self.name, f"Read does not read device or process files, and {path} is one.")
        resolved = await ws.canonical(path)
        if is_device_file(resolved):
            return tool_error(
                self.name, f"Read does not read device or process files, and {path} is one.")
        ext = posixpath.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return tool_error(
                self.name,
                f"Read does not open binary files, and a {ext} file is one. Open it with a "
                "skill made for the format, or with a shell command or script that "
                "understands it.")
        st = await env.stat(resolved)
        if st is None:
            return tool_error(self.name, await missing_file_message(ws, resolved))
        if st.is_dir:
            return tool_error(self.name, f"not a file: {path!r}")
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
        size = st.size
        chunks = _chunks(env, resolved)

        try:
            head = await env.read_range(resolved, 0, _SNIFF_BYTES)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")

        if ext == ".pdf" or (ext == "" and head.startswith(b"%PDF-")):
            return await read_pdf(self.name, env, resolved, display, size, input.get("pages"))

        if posixpath.splitext(resolved)[1] == ".ipynb":
            try:
                raw_nb = b"".join([c async for c in chunks()])
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            notebook = read_notebook(self.name, raw_nb, display)
            if not notebook.is_error:
                _record_read(ctx, resolved, hashlib.sha256(raw_nb).hexdigest(), st.mtime,
                             asked)
            return notebook

        image = detect_image(head)
        ext = posixpath.splitext(resolved)[1].lower()
        if ext in _IMAGE_EXTENSIONS and image is None:
            return tool_error(
                self.name,
                f"{display} has an image extension, but its bytes are not a PNG, JPEG, GIF or "
                "WebP image (often a saved error or login page). Check it with `file`, or "
                "look at it as text through Bash (`head -c 500`).")
        if image is not None and (ext in _IMAGE_EXTENSIONS or ext == ""):
            return await self._read_image(ctx, path, display, size, image, chunks)

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
        if not scan.capped and tokens > max_tokens and w.offset <= 1 and w.explicit_limit is None:
            return self._first_page(ctx, w, scan, text, tokens, max_tokens)
        if scan.capped or tokens > max_tokens:
            return tool_error(self.name, too_many_tokens(tokens, max_tokens))

        # Keyed by the canonical path and digesting the raw bytes of the whole
        # file, so a later mutation check compares like with like.
        _record_read(ctx, w.resolved, scan.digest, w.mtime, [w.offset, w.explicit_limit or 0])

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
                " To have all of it anyway, call Read again with allow_large: true (only when "
                "you need every part, or the user asked for the whole file).")
        head = (f"[Partial view, cut to fit the read limit: {w.resolved}")
        size = f"({tokens} tokens; the cap is {cap})"
        if not by_chars and shown < total:
            banner = (f"{head} shows lines 1-{shown} of {total} {size}. For the next part call "
                      f"Read with offset={shown + 1} limit={shown}, or search the file for the "
                      f"section you need.{hint} Do not answer from this part alone if the "
                      "answer could be further on.]")
        else:
            banner = (f"{head} shows its first {len(page)} of {len(text)} characters {size}; "
                      "its lines are too long to page by line. Search the file for the section "
                      f"you need, or page through it with Read's offset and limit.{hint} Do not "
                      "answer from this excerpt alone if the answer could be elsewhere.]")
        _record_read(ctx, w.resolved, scan.digest, w.mtime, [1, shown], partial=True)
        ctx.record(FS, KIND_NOTICE, {"text": banner})
        rendered = "\n".join(f"{i}\t{line}" for i, line in enumerate(page.split("\n"), 1))
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
    if scan.bytes_replaced:
        rendered += "\n\n" + _note("Non-utf8 bytes were replaced with U+FFFD.")
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
                 lines: list[int] | None = None, *, partial: bool = False) -> None:
    data: dict[str, Any] = {"path": path, "digest": digest, "mtime": mtime}
    if lines is not None:
        data["range"] = lines
    if partial:
        data["partial"] = True
    ctx.record(FS, KIND_READ, data)
