"""Where noeta keeps its own files: its home, the plans directory, the
per-project directory under the home, and its places in the temp directory."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from noeta.core.large_output import file_name_part as file_name_part

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


def temp_root() -> str:
    """noeta's directory in the machine's temp directory, for this OS user:
    ``<temp>/noeta-temp-<uid>`` (each worker process's sandbox directory,
    ``sandbox/``)."""
    return os.path.join(os.path.realpath(tempfile.gettempdir()), f"noeta-temp-{os.getuid()}")


def job_log_roots() -> list[str]:
    """Where the shell plugin may keep background job logs on this machine:
    ``noeta-background-<uid>`` in each temp directory it may pick."""
    bases = (os.environ.get("TMPDIR"), tempfile.gettempdir(), "/var/tmp", "/tmp")
    real = dict.fromkeys(os.path.realpath(b) for b in bases if b)
    return [os.path.join(b, f"noeta-background-{os.getuid()}") for b in real]


def private_dir(path: str, *, top: str | None = None) -> str:
    """Make ``path`` (and its parents below ``top``, an existing directory)
    as directories only this user may enter; raises ``OSError`` when one
    exists as something else: a link, a file, another user's."""
    path = os.path.abspath(path)
    top = os.path.abspath(top) if top else os.path.dirname(path)
    parts = os.path.relpath(path, top).split(os.sep)
    if parts[0] == "..":
        raise OSError(f"{path} is not inside {top}")
    current = top
    for part in parts:
        current = os.path.join(current, part)
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(current)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f"refusing {current}: not a directory owned by the current user")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(current, 0o700)
    return path

