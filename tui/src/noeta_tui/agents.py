"""Sub-agents, as Claude Code shows them: a list under the footer (``● main``
then each agent, what it is doing, how long and how many tokens), ↓ from an
empty prompt or a click to pick one, and a screen with what that agent did
(Esc goes back) in the transcript's place."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from noeta.core.events import InputTaken, ModelReplied, Say, TaskEnded, ToolFinished, ToolStarted
from noeta.core.types import ToolUse
from noeta_tui import render
from noeta_tui.widgets import (
    AssistantMessage,
    Choices,
    Notice,
    Option,
    ToolCall,
    UserMessage,
    bulleted,
)

log = logging.getLogger("noeta_tui")

#: The list's key for the main conversation.
MAIN = "main"
#: Agent rows shown at once; the list scrolls with the pick.
PANEL_ROWS = 5
#: Seconds between two reads of an open agent's log.
POLL = 0.5


def new_child(child_id: str, *, name: str = "", started: float | None = None) -> dict[str, Any]:
    """What is known of a sub-agent while it runs, read from its own log."""
    return {"id": child_id, "name": name, "tools": 0, "tokens": 0, "last": "",
            "started": started or time.time(), "status": "", "error": "", "answer": "",
            "took": None, "ended_at": None, "seq": 0}


def took(child: dict[str, Any]) -> float:
    return float(child["took"] if child["took"] is not None
                 else time.time() - child["started"])


def stats_text(seconds: float, tokens: int) -> str:
    """``1m 5s · ↓ 12.3k tokens``."""
    out = render.elapsed_text(seconds)
    return out + (f" · ↓ {render.tokens_text(tokens)} tokens" if tokens else "")


def child_activity(c: dict[str, Any]) -> tuple[str, str]:
    """What an agent is doing, or how it ended: ``(text, style)``."""
    if c["status"] == "done":
        return "Done", "dim"
    if c["status"] in ("failed", "cancelled"):
        what = "Stopped" if c["status"] == "cancelled" else "Failed"
        return what + (f": {c['error']}" if c["error"] else ""), "red"
    return c["last"] or "Starting…", "dim"


@dataclass
class Row:
    """One line of the list."""

    key: str
    name: str
    #: ``(+14)``: the agents a workflow's line stands for.
    extra: str = ""
    activity: str = ""
    activity_style: str = "dim"
    stats: str = ""
    #: ``running``, ``done`` or ``failed`` (``""`` for main).
    state: str = ""


class AgentsPanel(Static, can_focus=True):
    """The list under the footer while sub-agents run (or ran since the
    person last sent): ↑ ↓ pick, Enter or a click opens one, Esc (or ↑ past
    the top) goes back to the prompt."""

    BINDINGS = [Binding("up", "move(-1)", show=False), Binding("down", "move(1)", show=False),
                Binding("enter", "open", show=False), Binding("escape", "leave", show=False),
                Binding("right", "open", show=False)]

    def __init__(self, on_open: Callable[[str], Awaitable[None]],
                 on_leave: Callable[[], None]) -> None:
        super().__init__("", id="agents")
        self.on_open = on_open
        self.on_leave = on_leave
        self.rows: list[Row] = []
        self.index = 0
        #: The first agent row shown (the list scrolls with the pick).
        self.top = 0
        #: The row shown in the transcript's place (``●`` when the list is
        #: not focused).
        self.viewing = MAIN

    @property
    def agents(self) -> list[Row]:
        return self.rows[1:]

    def show(self, rows: list[Row]) -> None:
        """``rows``: the agents (main is added first)."""
        key = self.rows[self.index].key if self.rows else MAIN
        self.rows = [Row(MAIN, MAIN), *rows]
        self.index = next((n for n, r in enumerate(self.rows) if r.key == key), 0)
        self.display = bool(rows)
        if not rows and self.has_focus:
            self.on_leave()
        self._draw()

    def _shown(self) -> list[tuple[int, Row]]:
        """Main and the agent rows in view, by their index."""
        agents = self.rows[1:]
        if self.index > 0:
            if self.index - 1 < self.top:
                self.top = self.index - 1
            elif self.index - 1 >= self.top + PANEL_ROWS:
                self.top = self.index - PANEL_ROWS
        self.top = max(0, min(self.top, max(0, len(agents) - PANEL_ROWS)))
        return [(0, self.rows[0]), *((self.top + 1 + n, r) for n, r in
                                     enumerate(agents[self.top:self.top + PANEL_ROWS]))]

    def _draw(self) -> None:
        if not self.rows:
            self.update("")
            return
        grid = Table.grid(expand=True, padding=(0, 2, 0, 0))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(no_wrap=True, justify="right")
        focused = self.has_focus
        for n, row in self._shown():
            here = n == self.index if focused else row.key == self.viewing
            mark = "●" if here else "○"
            mark_style = {"failed": "red", "done": "green"}.get(row.state, "")
            name = Text()
            name.append(f"{mark} ", style=f"bold {render.ACCENT}" if here and focused
                        else mark_style or ("" if here else "dim"))
            name.append(render.clean(row.name), style=f"bold {render.ACCENT}" if here and focused
                        else "bold" if row.state in ("", "running") else "")
            if row.extra:
                name.append(f" {row.extra}", style="dim")
            grid.add_row(name, render.plain(row.activity, row.activity_style),
                         Text(row.stats, style="dim"))
        hidden = len(self.rows) - 1 - PANEL_ROWS
        if hidden > 0:
            grid.add_row(Text(f"  … {len(self.rows) - 1} agents (↑ ↓ to scroll)", style="dim"),
                         Text(""), Text(""))
        self.update(grid)

    def on_focus(self) -> None:
        if self.index == 0 and len(self.rows) > 1:
            self.index = 1
        self._draw()

    def on_blur(self) -> None:
        self._draw()

    def action_move(self, step: int) -> None:
        if not self.rows:
            return
        if step < 0 and self.index == 0:
            self.on_leave()
            return
        self.index = max(0, min(len(self.rows) - 1, self.index + step))
        self._draw()

    async def action_open(self) -> None:
        if self.rows:
            await self.on_open(self.rows[self.index].key)

    def action_leave(self) -> None:
        self.on_leave()

    async def on_click(self, event: events.Click) -> None:
        shown = self._shown()
        if 0 <= event.y < len(shown):
            self.index = shown[event.y][0]
            self._draw()
            await self.on_open(self.rows[self.index].key)


class AgentView(Vertical):
    """What one sub-agent did, from its own log, as the main transcript
    shows it: its prompt, what it said, each tool call and result; it
    follows the agent while it runs. It takes the transcript's place; Esc
    goes back."""

    DEFAULT_CLASSES = "agent-view"
    BINDINGS = [Binding("escape", "back", "Back", show=False),
                Binding("left", "back", show=False)]

    def __init__(self, child_id: str, title: str, *, kind: str = "",
                 child: dict[str, Any] | None = None, expanded: bool = False,
                 on_back: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self.child_id = child_id
        self.title_text = title
        self.kind = kind
        #: The app's live counts for the agent (``None``: not known).
        self.child = child
        self.expanded = expanded
        self.on_back = on_back
        self.calls: dict[str, ToolCall] = {}
        self.seq = 0
        self.ended = ""

    def compose(self) -> ComposeResult:
        yield Static("", classes="agent-head")
        yield VerticalScroll(classes="agent-log")
        yield Static(Text("Esc to go back · ↑ ↓ to scroll · ctrl+o to expand", style="dim"),
                     classes="agent-hint")

    def on_mount(self) -> None:
        self._head()
        self.query_one(".agent-log").focus()
        self._follow()
        self.set_interval(1.0, self._head)

    def _head(self) -> None:
        out = Text()
        out.append("← ", style="dim")
        out.append(render.clean(self.title_text), style="bold")
        if self.kind:
            out.append(render.clean(f"  {self.kind}"), style="dim")
        c = self.child
        if c is not None:
            out.append("  ·  ", style="dim")
            if c["status"]:
                text, style = child_activity(c)
                out.append(render.clean(text), style=style)
            else:
                out.append("running", style=render.WORKING)
            out.append(f"  ·  {render.agent_line(c['tools'], c['tokens'], took(c))}", style="dim")
        elif self.ended:
            out.append(render.clean(f"  ·  {self.ended}"), style="dim")
        self.query_one(".agent-head", Static).update(out)

    async def action_back(self) -> None:
        await self.on_back()

    def set_expanded(self, expanded: bool) -> None:
        self.expanded = expanded
        for call in self.calls.values():
            call.set_expanded(expanded)

    @work(exclusive=True, group="agent-log", exit_on_error=False)
    async def _follow(self) -> None:
        log_view = self.query_one(".agent-log", VerticalScroll)
        while True:
            try:
                found = await self.app.local.client.events(  # type: ignore[attr-defined]
                    self.child_id, self.seq)
            except Exception:
                log.debug("reading agent %s failed", self.child_id, exc_info=True)
                found = []
            at_end = log_view.scroll_y >= log_view.max_scroll_y - 1
            for rec in found:
                self.seq = rec.seq
                try:
                    await self._show(rec.event, log_view)
                except Exception:
                    log.exception("showing an agent's %s failed", type(rec.event).__name__)
            if found and at_end:
                log_view.scroll_end(animate=False)
            if self.ended:
                self._head()
                return
            await asyncio.sleep(POLL)

    async def _show(self, ev: Any, log_view: VerticalScroll) -> None:
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.text.strip():
                    await log_view.mount(UserMessage(item.body.message.text))
        elif isinstance(ev, ModelReplied):
            msg = ev.response.message
            if msg.text.strip():
                await log_view.mount(bulleted(AssistantMessage(msg.text)))
            for call in msg.tool_uses:
                block = ToolCall(call, expanded=self.expanded)
                self.calls[call.id] = block
                await log_view.mount(block)
        elif isinstance(ev, ToolStarted):
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            if (done := self.calls.get(ev.result.call_id)) is not None:
                done.set_result(ev.result.text, is_error=ev.result.is_error, meta=ev.meta)
        elif isinstance(ev, TaskEnded):
            self.ended = ev.status + (f": {ev.error}" if ev.error else "")
            await log_view.mount(Notice(f"agent {self.ended}",
                                        classes="-error" if ev.error else ""))


class WorkflowView(Vertical):
    """A workflow run's agents, to pick one from, in the transcript's
    place; Esc goes back."""

    DEFAULT_CLASSES = "agent-view"
    BINDINGS = [Binding("left", "back", show=False)]

    def __init__(self, title: str, rows: list[Row], *,
                 on_pick: Callable[[str], Awaitable[None]],
                 on_back: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self.title_text = title
        self.rows = rows
        self.on_pick = on_pick
        self.on_back = on_back

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("← ", "dim"), (render.clean(self.title_text), "bold"),
                                   (f"  {len(self.rows)} agents", "dim")), classes="agent-head")
        with VerticalScroll(classes="agent-log"):
            yield Choices([Option(r.key, r.name, f"{r.activity}  ·  {r.stats}")
                           for r in self.rows], self._picked, cancel="")
        yield Static(Text("Enter to open · Esc to go back", style="dim"), classes="agent-hint")

    def on_mount(self) -> None:
        self.query_one(Choices).focus()

    async def _picked(self, key: str) -> None:
        await (self.on_pick(key) if key else self.on_back())

    async def action_back(self) -> None:
        await self.on_back()


def agent_call_title(call: ToolUse) -> tuple[str, str]:
    """An ``Agent`` call's name in the list and its agent type."""
    name = str(call.input.get("description") or call.input.get("subagent_type") or "agent")
    return name, str(call.input.get("subagent_type") or "")
