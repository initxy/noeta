"""Images a typed prompt carries besides its text: a pasted image file or
the clipboard's image. Files it mentions with ``@path`` are attached inside
the task, where its files are (the file tools plugin)."""

from __future__ import annotations

import mimetypes
import shutil
import subprocess
from pathlib import Path

from noeta.core.types import Image, Message, Text

__all__ = ["IMAGE_TYPES", "attach", "clipboard_image", "image_file"]

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def image_file(path: Path) -> Image | None:
    """``path`` as an image block, when it is an image file."""
    kind = mimetypes.guess_type(path.name)[0]
    if kind not in IMAGE_TYPES or not path.is_file():
        return None
    return Image(media_type=kind, data=path.read_bytes())


def attach(text: str, images: list[Image]) -> Message:
    """The message for a typed prompt: its text, then the pasted images."""
    return Message(role="user", content=[Text(text=text), *images], origin="human")


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
