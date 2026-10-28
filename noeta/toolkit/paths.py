"""Where noeta keeps its own files: its home, the plans directory and the
per-project directory under the home."""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Longest project slug kept whole; a longer one is cut and given a hash.
PROJECT_SLUG_MAX = 200


def noeta_home() -> Path:
    """noeta's config directory: ``$NOETA_HOME``, else ``~/.noeta``."""
    return Path(os.environ.get("NOETA_HOME") or Path.home() / ".noeta")


def plans_dir() -> Path:
    """Where plan mode keeps plan files (Claude Code's ``plans`` directory
    in its config directory): file tools may write there."""
    return noeta_home() / "plans"


def _string_hash(text: str) -> int:
    """The 32-bit string hash Claude Code names long project slugs with
    (over UTF-16 code units)."""
    h = 0
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def project_slug(path: str) -> str:
    """``path`` as one directory name, as Claude Code names a project under
    ``projects/``: every character that is not an ASCII letter or digit
    becomes ``-``; past 200 characters the name is cut and a hash added."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(slug) <= PROJECT_SLUG_MAX:
        return slug
    return f"{slug[:PROJECT_SLUG_MAX]}-{_base36(abs(_string_hash(path)))}"


def project_dir(path: str, home: Path | None = None) -> Path:
    """The per-project directory for the workspace at ``path``:
    ``<home>/projects/<slug>`` (transcripts, auto memory)."""
    return (home or noeta_home()) / "projects" / project_slug(path)
