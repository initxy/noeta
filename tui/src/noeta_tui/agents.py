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
from textual.widget import Widget
from textual.widgets import Static

from noeta.core import records
from noeta.core.events import (
    ExtensionRecorded,
    InputTaken,
    ModelReplied,
    Say,
    TaskEnded,
    ToolFinished,
    ToolStarted,
)
from noeta.core.types import Thinking, ToolUse
from noeta.host.feed import failure_text
from noeta.host.rewind import lineage
from noeta_tui import render
from noeta_tui.tail import TailScroll
from noeta_tui.widgets import (
    AssistantMessage,
    Choices,
    Notice,
    Option,
    ThinkingBlock,
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
        #: Rows drawn last (``-1``: none yet).
        self._drawn_rows = -1

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
            self._drawn_rows = -1
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
        # Laid out again only when it gains or loses a row (update_status draws it often).
        rows = grid.row_count
        self.update(grid, layout=rows != self._drawn_rows)
        self._drawn_rows = rows

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
    """What one task did, from its own log, every call on its own line: a
    sub-agent's (its prompt, what it said, each tool call and result), or
    the main task's in full (``detail``: ctrl+o's detailed transcript,
    every result and thought unfolded, as Claude Code's transcript mode).
    It follows the task while it runs, in the transcript's place; Esc goes
    back."""

    DEFAULT_CLASSES = "agent-view"
    BINDINGS = [Binding("escape", "back", "Back", show=False),
                Binding("left", "back", show=False)]

    def __init__(self, child_id: str, title: str, *, kind: str = "",
                 child: dict[str, Any] | None = None, expanded: bool = False,
                 detail: bool = False,
                 on_back: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self.child_id = child_id
        self.title_text = title
        self.kind = kind
        #: The app's live counts for the agent (``None``: not known).
        self.child = child
        self.expanded = expanded or detail
        #: The main task's detailed transcript (ctrl+o), not a sub-agent's.
        self.detail = detail
        self.on_back = on_back
        self.calls: dict[str, ToolCall] = {}
        self.thoughts: list[ThinkingBlock] = []
        self.seq = 0
        self.ended = ""

    def compose(self) -> ComposeResult:
        if not self.detail:
            yield Static("", classes="agent-head")
        yield TailScroll(classes="agent-log")
        hint = "Showing detailed transcript · ctrl+o to toggle · ↑ ↓ to scroll" if self.detail \
            else "Esc to go back · ↑ ↓ to scroll · ctrl+o to expand"
        yield Static(Text(hint, style="dim"), classes="agent-hint")

    def on_mount(self) -> None:
        self._head()
        self.query_one(".agent-log").focus()
        self._follow()
        if not self.detail:
            self.set_interval(1.0, self._head)

    def _head(self) -> None:
        if self.detail:
            return
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
        for thought in self.thoughts:
            thought.set_expanded(expanded)

    async def _read(self) -> tuple[list[Any], list[Any]]:
        """What the task recorded after what is shown: the conversations it
        continues first (a rewind's fork's, read once, for the detailed
        transcript), then its own log."""
        client = self.app.local.client  # type: ignore[attr-defined]
        if self.detail and not self.seq:
            logs = await lineage(client, self.child_id)
            return [r for _, events in logs[:-1] for r in events], logs[-1][1]
        return [], await client.events(self.child_id, self.seq)

    @work(exclusive=True, group="agent-log", exit_on_error=False)
    async def _follow(self) -> None:
        log_view = self.query_one(".agent-log", TailScroll)
        first = True
        while True:
            try:
                earlier, found = await self._read()
            except Exception:
                log.debug("reading %s failed", self.child_id, exc_info=True)
                earlier, found = [], []
            shown: list[Widget] = []
            for rec in [*earlier, *found]:
                try:
                    shown += self._show(rec.event)
                except Exception:
                    log.exception("showing a task's %s failed", type(rec.event).__name__)
            if found:
                self.seq = found[-1].seq
            if first:  # the log so far at once, only its end laid out
                await log_view.show_all(shown)
                log_view.scroll_end(animate=False)
                self.call_after_refresh(log_view.stick)
                first = False
            elif shown:
                await log_view.mount_all(shown)
                log_view.stick()
            if self.ended:
                self._head()
                return
            await asyncio.sleep(POLL)

    def _show(self, ev: Any) -> list[Widget]:
        """The widgets ``ev`` adds (a call's start or end changes its own)."""
        out: list[Widget] = []
        if isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Say) and item.body.message.text.strip() and \
                        (not self.detail or item.body.message.origin in (None, "human")):
                    out.append(UserMessage(item.body.message.text))
        elif isinstance(ev, ModelReplied):
            msg = ev.response.message
            for block in msg.content:
                if isinstance(block, Thinking) and block.text.strip():
                    thought = ThinkingBlock(expanded=self.expanded)
                    thought.text = block.text
                    thought.finish(0.0)
                    self.thoughts.append(thought)
                    out.append(thought)
            if msg.text.strip():
                out.append(bulleted(AssistantMessage(msg.text)))
            for call in msg.tool_uses:
                tool = ToolCall(call, expanded=self.expanded,
                                cwd=str(getattr(self.app, "task_cwd", "") or ""))
                self.calls[call.id] = tool
                out.append(tool)
        elif isinstance(ev, ToolStarted):
            if ev.call.id in self.calls:
                self.calls[ev.call.id].set_state("running")
        elif isinstance(ev, ToolFinished):
            if (done := self.calls.get(ev.result.call_id)) is not None:
                done.set_result(ev.result.text, is_error=ev.result.is_error, meta=ev.meta)
        elif isinstance(ev, ExtensionRecorded):
            core = records.decode(ev)
            if isinstance(core, records.TurnStopped):
                out.append(Notice(Text("  ⎿  Interrupted", style="red")))
            elif isinstance(core, records.TurnFailed):
                data = ev.data if isinstance(ev.data, dict) else {}
                out.append(Notice(f"turn failed: {failure_text(data)}", classes="-error"))
            elif isinstance(core, records.Notice) and self.detail:
                out.append(Notice(core.text))
        elif isinstance(ev, TaskEnded):
            self.ended = ev.status + (f": {ev.error}" if ev.error else "")
            out.append(Notice(f"{'task' if self.detail else 'agent'} {self.ended}",
                              classes="-error" if ev.error else ""))
        return out


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
