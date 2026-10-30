"""Make an image fit what a model request accepts, with Claude Code's limits.

An image enters a task's append-only history once and is sent on every later
request, so one the API refuses (over 5 MB in base64, or too many pixels)
would fail every turn after it. :func:`fit_image` shrinks an image to at most
2000 x 2000 px and 3.75 MB raw (5 MB once base64 encoded) when Pillow is
installed (the ``images`` extra), the way Claude Code does; without it, an
image that is already small enough passes unchanged and any other is refused
with :class:`ImageTooLarge` before it reaches the history.

Every image a person sends goes through :func:`fit_message` once, in the
client's create and send, so each front end (the TUI, ACP, stream-json, HTTP)
is covered by the same code; tools fit the images they return themselves.
"""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from typing import Any

from noeta.core.types import Image, Message, Text

__all__ = [
    "MAX_BASE64_BYTES",
    "MAX_HEIGHT",
    "MAX_WIDTH",
    "TARGET_RAW_BYTES",
    "UNSHRUNK_MAX_SIDE",
    "FittedImage",
    "ImageTooLarge",
    "base64_size",
    "fit_image",
    "fit_message",
    "image_dimensions",
]

#: Claude Code's image limits: the box an image is shrunk into, the API's
#: base64 ceiling, and the raw size whose base64 form just fits it.
MAX_WIDTH = 2000
MAX_HEIGHT = 2000
MAX_BASE64_BYTES = 5 * 1024 * 1024
TARGET_RAW_BYTES = MAX_BASE64_BYTES * 3 // 4

#: Without an image library nothing is shrunk; the API still takes an image
#: up to this many pixels on a side.
UNSHRUNK_MAX_SIDE = 8000

_JPEG_QUALITIES = (80, 60, 40, 20)
_LAST_RESORT_WIDTH = 1000


class ImageTooLarge(ValueError):
    """The image cannot be made to fit; the message says why and what to do."""


@dataclass(frozen=True)
class FittedImage:
    data: bytes
    media_type: str
    #: ``(width, height)`` before and after, when known.
    original: tuple[int, int] | None = None
    shown: tuple[int, int] | None = None

    @property
    def resized(self) -> bool:
        return self.original is not None and self.original != self.shown

    def note(self) -> str | None:
        """A line telling the model how the pixels it sees map back to the
        file, when the image was scaled."""
        if not self.resized or self.original is None or self.shown is None:
            return None
        (w, h), (sw, sh) = self.original, self.shown
        return (f"The image was scaled from {w}x{h} to {sw}x{sh} px; multiply coordinates by "
                f"{w / sw:.2f} to map them to the original.")


def base64_size(raw_bytes: int) -> int:
    return 4 * math.ceil(raw_bytes / 3)


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def image_dimensions(raw: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG, GIF, JPEG or WebP header, or ``None``."""
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[12:16] == b"IHDR":
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", raw[6:10])
            return int(w), int(h)
        if raw.startswith(b"\xff\xd8"):
            return _jpeg_dimensions(raw)
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return _webp_dimensions(raw)
    except struct.error:
        return None
    return None


def _jpeg_dimensions(raw: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 4 <= len(raw):
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", raw[i + 2:i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", raw[i + 5:i + 9])
            return int(w), int(h)
        i += 2 + length
    return None


def _webp_dimensions(raw: bytes) -> tuple[int, int] | None:
    chunk = raw[12:16]
    if chunk == b"VP8X":
        w = int.from_bytes(raw[24:27], "little") + 1
        h = int.from_bytes(raw[27:30], "little") + 1
        return w, h
    if chunk == b"VP8L":
        bits = int.from_bytes(raw[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", raw[26:30])
        return int(w) & 0x3FFF, int(h) & 0x3FFF
    return None


def _target(width: int, height: int) -> tuple[int, int]:
    if width > MAX_WIDTH:
        height, width = round(height * MAX_WIDTH / width), MAX_WIDTH
    if height > MAX_HEIGHT:
        width, height = round(width * MAX_HEIGHT / height), MAX_HEIGHT
    return max(width, 1), max(height, 1)


def fit_image(raw: bytes, media_type: str) -> FittedImage:
    """``raw`` made to fit, or :class:`ImageTooLarge`."""
    if not raw:
        raise ImageTooLarge("the image file is empty (0 bytes)")
    try:
        from PIL import Image as _pil  # noqa: F401
    except ImportError:
        return _pass_through(raw, media_type)
    return _shrink(raw, media_type)


def fit_message(message: Message) -> Message:
    """``message`` with every image made to fit; one that cannot be is
    replaced by a line saying it was left out and why."""
    if not any(isinstance(b, Image) for b in message.content):
        return message
    content: list[Any] = []
    for block in message.content:
        if not isinstance(block, Image):
            content.append(block)
            continue
        try:
            fitted = fit_image(block.data, block.media_type)
        except ImageTooLarge as exc:
            content.append(Text(text=f"[An attached image was left out: {exc}.]"))
            continue
        content.append(block if fitted.data == block.data and fitted.media_type == block.media_type
                       else Image(media_type=fitted.media_type, data=fitted.data))
    return Message(role=message.role, content=content, origin=message.origin)


def _pass_through(raw: bytes, media_type: str) -> FittedImage:
    dims = image_dimensions(raw)
    if dims is None:
        raise ImageTooLarge(
            "its dimensions could not be read from the file header and no image library is "
            "installed to check them; convert it to PNG, JPEG, GIF or WebP")
    w, h = dims
    if w > UNSHRUNK_MAX_SIDE or h > UNSHRUNK_MAX_SIDE:
        raise ImageTooLarge(
            f"at {w}x{h} px it is over the {UNSHRUNK_MAX_SIDE}x{UNSHRUNK_MAX_SIDE} px limit and "
            "this host cannot shrink images (install noeta[images]); resize it to fit "
            f"within {MAX_WIDTH}x{MAX_HEIGHT} px and try again")
    encoded = base64_size(len(raw))
    if encoded > MAX_BASE64_BYTES:
        raise ImageTooLarge(
            f"it is {_mb(encoded)} in base64, over the {_mb(MAX_BASE64_BYTES)} limit, and this "
            "host cannot shrink images (install noeta[images]); make it smaller than "
            f"{_mb(TARGET_RAW_BYTES)} and try again")
    return FittedImage(raw, media_type, dims, dims)


def _shrink(raw: bytes, media_type: str) -> FittedImage:
    from PIL import Image, UnidentifiedImageError

    try:
        img: Any = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if base64_size(len(raw)) <= MAX_BASE64_BYTES and (
                dims := image_dimensions(raw)) is not None and max(dims) <= MAX_WIDTH:
            return FittedImage(raw, media_type, dims, dims)
        raise ImageTooLarge(f"it could not be decoded to shrink it ({exc}); re-save it as a "
                            "PNG or JPEG within "
                            f"{MAX_WIDTH}x{MAX_HEIGHT} px and try again") from None
    original = (int(img.width), int(img.height))
    target = _target(*original)
    fmt = (img.format or "").lower()
    if len(raw) <= TARGET_RAW_BYTES and target == original:
        return FittedImage(raw, media_type, original, original)
    is_png = fmt == "png"
    if target != original:
        img = img.resize(target, Image.Resampling.LANCZOS)
        out_fmt = {"jpeg": "jpeg", "webp": "webp"}.get(fmt, "png")
        data = _encode(img, out_fmt)
        if len(data) <= TARGET_RAW_BYTES:
            return FittedImage(data, f"image/{out_fmt}", original, target)
    for attempt in _smaller(img, is_png):
        if len(attempt[0]) <= TARGET_RAW_BYTES:
            return FittedImage(attempt[0], attempt[1], original, target)
    width = min(target[0], _LAST_RESORT_WIDTH)
    last = (width, max(round(target[1] * width / max(target[0], 1)), 1))
    data = _encode(img.resize(last, Image.Resampling.LANCZOS), "jpeg", quality=20)
    return FittedImage(data, "image/jpeg", original, last)


def _smaller(img: Any, is_png: bool) -> Any:
    if is_png:
        yield _encode(img, "png", palette=True), "image/png"
    for quality in _JPEG_QUALITIES:
        yield _encode(img, "jpeg", quality=quality), "image/jpeg"


def _encode(img: Any, fmt: str, *, quality: int = 90, palette: bool = False) -> bytes:
    buf = io.BytesIO()
    if fmt == "jpeg":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(buf, "JPEG", quality=quality)
    elif fmt == "png":
        if palette:
            img = img.convert("RGBA").quantize(256)
        img.save(buf, "PNG", optimize=True)
    else:
        img.save(buf, fmt.upper())
    return buf.getvalue()
