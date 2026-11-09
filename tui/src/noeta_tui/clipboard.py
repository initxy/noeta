"""Putting copied text on the person's clipboard, wherever the terminal runs:
OSC 52 always (the terminal's own clipboard; over ssh too); inside tmux also
tmux's buffer, which tmux passes on to the terminal (its default
``set-clipboard external`` ignores an app's OSC 52); on this machine (not
over ssh) also its clipboard program, for a terminal that ignores OSC 52."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import Any

from textual.app import App

log = logging.getLogger("noeta_tui")

#: Seconds a clipboard program may take.
TIMEOUT = 2.0
#: tmux's buffer, sent on to the terminal (``-w``: tmux 3.2 and later; an
#: older one takes the buffer only).
TMUX = (["tmux", "load-buffer", "-w", "-"], ["tmux", "load-buffer", "-"])
#: The local clipboard programs, the first found is used: each with the
#: variable its display server sets (``""``: none needed).
LOCAL = ((["pbcopy"], ""), (["wl-copy"], "WAYLAND_DISPLAY"),
         (["xclip", "-selection", "clipboard"], "DISPLAY"),
         (["xsel", "--clipboard", "--input"], "DISPLAY"))
#: Set in a shell reached over ssh.
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")


def copy(app: App[Any], text: str) -> None:
    """``text`` to the clipboard: OSC 52 at once, the programs in the
    background (a failure is logged, never raised)."""
    app.copy_to_clipboard(text)
    targets = programs(os.environ, shutil.which)
    if targets:
        threading.Thread(target=_deliver, args=(targets, text), name="clipboard",
                         daemon=True).start()


def programs(env: Mapping[str, str], which: Callable[[str], str | None]
             ) -> list[tuple[list[str], ...]]:
    """The programs that take the text besides OSC 52, each as the commands
    tried in turn until one works."""
    found: list[tuple[list[str], ...]] = []
    if env.get("TMUX"):
        found.append(TMUX)
    if not any(env.get(name) for name in SSH_VARIABLES):
        local = next((command for command, needs in LOCAL
                      if (not needs or env.get(needs)) and which(command[0])), None)
        if local is not None:
            found.append((local,))
    return found


def _deliver(targets: list[tuple[list[str], ...]], text: str) -> None:
    data = text.encode("utf-8")
    for commands in targets:
        for command in commands:
            if _run(command, data):
                break


def _run(command: list[str], data: bytes) -> bool:
    """Whether ``command`` took ``data`` on its stdin. ``xclip`` and
    ``wl-copy`` leave a process serving the clipboard: its output goes
    nowhere (it would hold a pipe open) and it runs in its own session (it
    outlives the terminal)."""
    try:
        done = subprocess.run(command, input=data, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False,
                              start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        log.debug("copying with %s failed", command[0], exc_info=True)
        return False
    if done.returncode:
        log.debug("copying with %s exited %s", " ".join(command), done.returncode)
    return done.returncode == 0
