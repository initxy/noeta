"""What the ``noeta`` command does with an error it did not expect: one line
naming it and a report file (traceback, version, command line) under
``~/.noeta/logs/``, instead of a Python traceback on the terminal."""

from __future__ import annotations

import os
import platform
import sys
import time
import traceback
from pathlib import Path

from noeta.cli import home_dir

#: Set to a true value, the traceback is printed on the terminal too.
DEBUG_ENV = "NOETA_DEBUG"


def debug(argv: list[str]) -> bool:
    """Whether the person asked for tracebacks: ``--verbose`` on the command
    line or ``NOETA_DEBUG`` set (``-v`` is ``--version``, as in Claude Code)."""
    flag = os.environ.get(DEBUG_ENV, "").strip().lower()
    return "--verbose" in argv or flag not in ("", "0", "false", "no")


def write_report(exc: BaseException, argv: list[str]) -> Path | None:
    """The crash report of ``exc`` written under ``~/.noeta/logs/`` (owner
    only), or ``None`` when it cannot be written."""
    from noeta.host.stream_json import noeta_version

    text = "".join(traceback.format_exception(exc))
    body = (f"noeta {noeta_version()}\n"
            f"python {platform.python_version()} on {platform.platform()}\n"
            f"argv: {sys.argv[0]} {' '.join(argv)}\n"
            f"time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n\n{text}")
    logs = home_dir.HOME / "logs"
    path = logs / f"crash-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
    try:
        home_dir.ensure_home()
        logs.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError:
        return None
    return path


def crash(exc: BaseException, argv: list[str]) -> int:
    """Tell the person about ``exc`` on stderr in a line or two, write its
    report, and return the exit code (1)."""
    message = next((line for line in str(exc).splitlines() if line.strip()), "")
    head = f"noeta: unexpected error: {type(exc).__name__}" + (f": {message}" if message else "")
    path = write_report(exc, argv)
    shown = debug(argv) or path is None
    if shown:
        traceback.print_exception(exc, file=sys.stderr)
    print(head, file=sys.stderr)
    if path is not None:
        more = "" if shown else f" (--verbose or {DEBUG_ENV}=1 prints it here)"
        print(f"noeta: the full report is in {path}{more}", file=sys.stderr)
    return 1
