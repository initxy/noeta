"""``!command``: Claude Code's bash mode. The command runs here, in the
task's directory, its output is shown, and it goes with the next message
in Claude Code's ``<bash-input>`` / ``<bash-stdout>`` tags, so the model
sees what the person ran."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

from textual.widgets import Static

from noeta_tui import render
from noeta_tui.widgets import UserMessage

if TYPE_CHECKING:
    from noeta_tui.app import NoetaApp

#: Seconds a command may run.
TIMEOUT = 120
#: Characters of its output the next message carries.
CONTEXT_CHARS = 30000


def _run(command: str, cwd: str) -> tuple[str, str, int]:
    try:
        done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                              errors="replace", timeout=TIMEOUT, check=False,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "", f"timed out after {TIMEOUT}s", 124
    except OSError as exc:
        return "", str(exc), 127
    return done.stdout, done.stderr, done.returncode


def context_text(command: str, out: str, err: str) -> str:
    """What the next message carries of a command the person ran."""
    return (f"<bash-input>{command}</bash-input>\n"
            f"<bash-stdout>{out[:CONTEXT_CHARS]}</bash-stdout>"
            f"<bash-stderr>{err[:CONTEXT_CHARS]}</bash-stderr>")


async def run(app: NoetaApp, command: str) -> None:
    if not command:
        return
    transcript = app.transcript()
    await transcript.mount(UserMessage(command, marker="!"))
    if app.on_server:
        await app.say_notice("bash mode runs commands on this machine; attached to a "
                             "server, the task's files are on the server")
        return
    out, err, code = await asyncio.to_thread(_run, command, app.task_cwd or app.cwd)
    rows = [(line, "") for line in out.rstrip("\n").splitlines()]
    rows += [(line, "red") for line in err.rstrip("\n").splitlines()]
    if not rows:
        rows = [("(No content)" if code == 0 else f"exit code {code}", "dim")]
    await transcript.mount(Static(render.result_block(
        render.folded(rows, diff=False, expanded=app.verbose)), classes="command-output"))
    transcript.stick()
    app.bash_context.append(context_text(command, out, err))
