"""What noeta logs at warning level and above, on stderr as one line each:
``noeta: warning: …`` / ``noeta: error: …``. On a terminal the label is
coloured (yellow, red) and a warning's text dimmed, so it stands apart from
a command's own output; ``NO_COLOR`` or a dumb terminal keeps it plain. The
terminal UI takes these lines over (its screen covers stderr)."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

__all__ = ["StderrLines", "install", "uninstall"]

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_LABEL = {"warning": "\x1b[1;33m", "error": "\x1b[1;31m"}


def _coloured(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


class StderrLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string is still a line
            text = str(record.msg)
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and str(exc) and str(exc) not in text:
            text += f": {exc}"
        label = "error" if record.levelno >= logging.ERROR else "warning"
        stream = sys.stderr  # read now: whoever replaced it since gets the line
        if _coloured(stream):
            body = text if label == "error" else f"{_DIM}{text}{_RESET}"
            line = f"{_DIM}noeta:{_RESET} {_LABEL[label]}{label}:{_RESET} {body}"
        else:
            line = f"noeta: {label}: {text}"
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 — a closed stderr loses the line, not the run
            pass


def install() -> None:
    """Put the lines on stderr (once, however often it is called)."""
    root = logging.getLogger()
    if not any(isinstance(h, StderrLines) for h in root.handlers):
        root.addHandler(StderrLines())


def uninstall() -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, StderrLines)]:
        root.removeHandler(handler)
