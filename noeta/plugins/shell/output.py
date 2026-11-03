"""How a ``Bash`` command's run becomes its result: which exit statuses
fail, the merged streams, the capture-cap notice and an image printed as a
``data:`` URI."""

from __future__ import annotations

import base64
import binascii
import re

from noeta.core.execenv import RunResult
from noeta.core.images import ImageTooLarge, fit_image
from noeta.core.types import Image
from noeta.toolkit.shell_syntax import parse

#: Commands whose exit status 1 is an answer, not a failure (no match, a
#: difference, a false condition), with what it means: Claude Code's table.
#: Status 2 and up still fail.
_EXIT_ONE_ANSWERS = {"grep": "No matches found", "rg": "No matches found",
                     "egrep": "No matches found", "fgrep": "No matches found",
                     "find": "Some directories were inaccessible", "diff": "Files differ",
                     "test": "Condition is false", "[": "Condition is false"}
#: ``git`` subcommands read the same way.
_GIT_EXIT_ONE_ANSWERS = {"diff": "Files differ", "grep": "No matches found"}
#: Where a command line is split into its last command when it cannot be
#: parsed.
_COMMAND_SEPARATOR = re.compile(r"&&|\|\||[;|\n]")


def _last_command(command: str) -> list[str]:
    """The words of the command line's last command (what decides its exit
    status), by the parser when it can read the line, else by splitting on
    the control operators."""
    parsed = parse(command)
    if not parsed.opaque:
        return parsed.commands[-1].argv if parsed.commands else []
    return _COMMAND_SEPARATOR.split(command)[-1].split()


def _exit_one_answer(command: str) -> str | None:
    """What exit status 1 of ``command`` means when its last command is one
    whose status 1 is an answer; ``None`` otherwise."""
    argv = _last_command(command)
    name = argv[0] if argv else ""
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in ("-C", "-c") else 1
        return _GIT_EXIT_ONE_ANSWERS.get(argv[i]) if i < len(argv) else None
    return _EXIT_ONE_ANSWERS.get(name)


def exit_fails(command: str, exit_code: int) -> bool:
    """Whether ``exit_code`` makes ``command`` an error result."""
    if exit_code == 0:
        return False
    return exit_code != 1 or _exit_one_answer(command) is None


def background_exit_note(command: str, exit_code: int | None) -> str | None:
    """What a background command's non-zero ``exit_code`` means when it is
    an answer rather than a failure (the job then completed); ``None`` when
    it failed. Stricter than :func:`exit_fails`, as Claude Code is for a
    background command: when the last command follows ``&&``, or the line
    cannot be read, status 1 may come from an earlier command, so it fails."""
    if exit_code != 1:
        return None
    parsed = parse(command)
    if parsed.opaque or (parsed.commands and parsed.commands[-1].before == "&&"):
        return None
    return _exit_one_answer(command)


_IMAGE_URI = re.compile(r"data:image/[a-z0-9.+_-]+;base64,(.+)", re.IGNORECASE | re.DOTALL)
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def output_image(body: str) -> Image | None:
    """An image a command printed as one ``data:image/...;base64,`` URI
    (Claude Code shows such output as the picture), typed by its bytes and
    fitted to the size limits; ``None`` for anything else."""
    match = _IMAGE_URI.fullmatch(body.strip())
    if match is None:
        return None
    try:
        raw = base64.b64decode("".join(match.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    media = next((m for magic, m in _IMAGE_MAGIC if raw.startswith(magic)), None)
    if media is None and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        media = "image/webp"
    if media is None:
        return None
    try:
        fitted = fit_image(raw, media)
    except ImageTooLarge:
        return None
    return Image(media_type=fitted.media_type, data=fitted.data)


def render_streams(stdout: bytes, stderr: bytes) -> str:
    """The merged output (stderr is redirected into stdout; anything left on
    stderr follows), trailing newlines trimmed."""
    text = (stdout + stderr).decode("utf-8", errors="replace")
    return text.rstrip("\n")


def capture_notice(result: RunResult, output_cap: int) -> str:
    """Name a stream whose HEAD the capture cap dropped, or ``""``."""
    if not (result.stdout_truncated or result.stderr_truncated):
        return ""
    size = f"{output_cap // 1024} KB" if output_cap >= 1024 else f"{output_cap} bytes"
    return f"[earlier output dropped: only the last {size} was captured]"
