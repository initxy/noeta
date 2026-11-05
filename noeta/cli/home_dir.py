"""noeta's home directory (``$NOETA_HOME``, else ``~/.noeta``): where it
is, making it owner-only, and this machine's device id kept in it. Every
module reads :data:`HOME` from here at call time, so tests set it once."""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

HOME = Path(os.environ.get("NOETA_HOME") or Path.home() / ".noeta")
#: The skills directory coding agents share (``~/.agents/skills``), not
#: noeta's own: ``$NOETA_HOME`` does not move it.
AGENTS_SKILLS = Path.home() / ".agents" / "skills"


def ensure_home() -> Path:
    """noeta's home, made owner-only (0700) when noeta creates it: it holds
    the store, tokens and every setting. An existing one keeps its mode
    (:func:`loose_permissions` says when it is open to others)."""
    HOME.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        HOME.mkdir(mode=0o700)
    return HOME


def _sqlite_file(url: str) -> str | None:
    """The database file of a ``sqlite:///path`` store; ``None`` for
    another store."""
    path = url.removeprefix("sqlite:///") if url.startswith("sqlite:///") else ""
    if not path or path == ":memory:":
        return None
    return os.path.abspath(os.path.expanduser(path))


def loose_permissions(store_url: str) -> list[str]:
    """A notice for noeta's home and a sqlite store's database file when
    other users may read them; noeta never changes an existing mode."""
    db = _sqlite_file(store_url)
    places = [(HOME, "700")] + ([(Path(db), "600")] if db else [])
    notices = []
    for path, wanted in places:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:
            notices.append(f"{path} is open to other users (mode {mode:o}) and holds your "
                           f"conversations and settings: chmod {wanted} {path}")
    return notices


def device_id() -> str | None:
    """This machine's random id, made on first use (``~/.noeta/device_id``):
    the stable device id Claude Code sends in ``metadata.user_id``. ``None``
    when it cannot be kept (the provider then uses one per process)."""
    path = HOME / "device_id"
    try:
        text = path.read_text("utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        ensure_home()
        path.write_text(value + "\n", "utf-8")
    except OSError:
        return None
    return value
