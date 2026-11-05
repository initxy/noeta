"""What the terminal UI's tests share: an app on a scripted model, a tool and
gates to drive it, a real PNG, waiting for the UI, and reading what it shows."""

from __future__ import annotations

import asyncio
import inspect
import struct
import time
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.extension import Ask, Command, CommandContext, Extension, TaskView
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ToolUse
from noeta.host.app import FrontEndStart
from noeta.host.local import LocalApp
from noeta.plugins.fs import FileTools
from noeta_tui.app import NoetaApp


def png(width: int = 2, height: int = 2) -> bytes:
    """A valid grey PNG of ``width`` x ``height``."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


async def settle(pilot: Any, cond: Any, timeout: float = 20.0) -> None:
    """Wait until ``cond()`` holds (it may return an awaitable) while the
    app runs: never a fixed sleep, ``timeout`` seconds at most."""
    deadline = time.monotonic() + timeout
    while True:
        held = cond()
        if inspect.isawaitable(held):
            held = await held
        if held:
            return
        if time.monotonic() > deadline:
            raise AssertionError("UI did not settle")
        await pilot.pause(0.02)


def ready(app: Any) -> bool:
    """The app shows its first screen (a new task's: no task yet)."""
    from noeta_tui.widgets import Welcome

    return bool(app.query(Welcome))


def plain(widget: Any) -> str:
    shown = widget.render()
    return str(getattr(shown, "plain", shown))


def shown(app: Any) -> str:
    """Every text on the screen: notices, commands' output, pickers, markdown."""
    from textual.widgets import Markdown, Static

    out = [plain(w) for w in app.query(Static)]
    out += [m.source for m in app.query(Markdown)]
    return "\n".join(out)


async def type_line(pilot: Any, text: str) -> None:
    pilot.app.query_one("#prompt").text = text
    await pilot.press("enter")


MODEL = "claude-opus-5-5"


class Lines:
    """A shell-like tool printing ``n`` lines."""

    name = "Bash"
    description = "Run."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.release.set()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        await self.release.wait()
        return text_output("\n".join(f"line {i}" for i in range(1, int(input["n"]) + 1)))


class Where(Extension):
    name = "where"

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="where", description="Where am I", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return f"you are in {ctx.cwd}"


class AskBash(Extension):
    name = "ask"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="run it?") if call.name == "Bash" else None


def make_app(tmp_path: Path, provider: Any, *exts: Extension, tools: Sequence[Any] = (),
             settings: Any = None, **start: Any) -> NoetaApp:
    """``start``: more of what the ``noeta`` command hands the app
    (``FrontEndStart``'s ``permission_mode``, ``trust``, ``prompt``)."""
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    agent = Agent(name="main", model=MODEL, tools=list(tools),
                  extensions=[FileTools(), Where(), *exts])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: LocalExecEnv(str(work)), own_tasks_only=True)
    front = FrontEndStart(agent="main", cwd=str(work), model=MODEL, version="1.2.3",
                          home=str(tmp_path / "home"),
                          settings=settings or (lambda: [("store", "sqlite:///t.db")]),
                          **start)
    return NoetaApp(local, agent="main", model=MODEL, cwd=str(work),
                    permission_mode=start.get("permission_mode"),
                    resume=start.get("resume"), start=front)
