"""What a typed prompt carries besides its text: files it mentions with
``@path`` (attached as the Read tool would show them, as Claude Code
attaches them) and images (a pasted image file or the clipboard's image)."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
from pathlib import Path

from noeta.core.types import Image, Message, Text

__all__ = ["IMAGE_TYPES", "attach", "clipboard_image", "image_file", "mentioned"]

#: ``@path`` or ``@"path with spaces"``, not inside a word or an e-mail address.
MENTION = re.compile(r'(?<![\w@])@("([^"]+)"|[^\s"]+)')
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
#: An attached file is cut to these, as Read cuts a file read without a range.
MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 200


def mentioned(text: str, cwd: str) -> list[Path]:
    """The existing files and directories ``text`` mentions, in order."""
    out: list[Path] = []
    for m in MENTION.finditer(text):
        raw = (m.group(2) or m.group(1)).rstrip(".,;:!?)")
        path = Path(raw).expanduser()
        path = path if path.is_absolute() else Path(cwd) / path
        if path.exists() and path not in out:
            out.append(path.resolve())
    return out


def image_file(path: Path) -> Image | None:
    """``path`` as an image block, when it is an image file."""
    kind = mimetypes.guess_type(path.name)[0]
    if kind not in IMAGE_TYPES or not path.is_file():
        return None
    return Image(media_type=kind, data=path.read_bytes())


def _numbered(path: Path) -> str:
    data = path.read_bytes()[:MAX_BYTES]
    lines = data.decode("utf-8", errors="replace").splitlines()
    shown = [f"{n}\t{line[:MAX_LINE_CHARS]}" for n, line in enumerate(lines[:MAX_LINES], 1)]
    if len(lines) > MAX_LINES or path.stat().st_size > MAX_BYTES:
        shown.append(f"(the file goes on; read it with offset {len(shown) + 1} for more)")
    return "\n".join(shown)


def _attachment(path: Path) -> Text | Image:
    image = image_file(path)
    if image is not None:
        return image
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        body = "\n".join(entries[:MAX_ENTRIES])
        if len(entries) > MAX_ENTRIES:
            body += f"\n(and {len(entries) - MAX_ENTRIES} more)"
        return Text(text=f"The user mentioned the directory {path}. It holds:\n{body}")
    call = json.dumps({"file_path": str(path)})
    return Text(text=f"The user mentioned {path}; here it is as Read {call} returns it:\n"
                     f"{_numbered(path)}")


def attach(text: str, cwd: str, images: list[Image] | None = None) -> Message:
    """The message for a typed prompt: its text, each mentioned file or
    directory, then the pasted images."""
    content: list[Text | Image] = [Text(text=text)]
    for path in mentioned(text, cwd):
        try:
            content.append(_attachment(path))
        except OSError:
            continue
    content.extend(images or ())
    return Message(role="user", content=list(content), origin="human")


def clipboard_image() -> Image | None:
    """The image on the clipboard (``wl-paste``, ``xclip`` or ``pngpaste``),
    or ``None``."""
    for argv in (["wl-paste", "--type", "image/png"],
                 ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                 ["pngpaste", "-"]):
        if shutil.which(argv[0]) is None:
            continue
        try:
            done = subprocess.run(argv, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.startswith(b"\x89PNG"):
            return Image(media_type="image/png", data=done.stdout)
    return None
