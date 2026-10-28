"""The one extension interface.

Everything beyond the core loop (permissions, subtasks, compaction, memory,
skills, MCP, background shells) is an ``Extension``. Every method is
optional. An extension keeps durable state by recording
``ExtensionRecorded`` entries (``view.record``) and folding them in
``reduce``; the core never grows an event type for an extension.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Union

from noeta.core.events import Compacted, Event, ExtensionRecorded, Recorded
from noeta.core.execenv import ExecEnv
from noeta.core.tool import Tool, ToolOutput
from noeta.core.types import Message, ModelRequest, ModelResponse, Struct, ToolUse

if TYPE_CHECKING:
    from noeta.core.agent import Agent
    from noeta.core.loop.state import TaskState

log = logging.getLogger("noeta.plugins")


class Allow(Struct, tag="allow", tag_field="gate"):
    pass


class Deny(Struct, tag="deny", tag_field="gate"):
    reason: str = ""


class Ask(Struct, tag="ask", tag_field="gate"):
    """Park the call until someone answers. ``prompt`` is what a front end
    shows; ``options`` lets an extension offer more than yes/no."""

    prompt: str = ""
    detail: dict[str, Any] = {}


class Rewrite(Struct, tag="rewrite", tag_field="gate"):
    """Run the call with different input (or a different tool)."""

    input: dict[str, Any]
    name: str | None = None


Gate = Union[Allow, Deny, Ask, Rewrite]


class ContextPart(Struct):
    """System prompt text an extension contributes: it continues the agent's
    prompt, after a newline, in ``order``, inside the prompt's one cached
    block. Text that must sit in the conversation instead is recorded as a
    message (``TaskView.record(..., message=...)``) so the request prefix
    stays byte-stable."""

    text: str
    order: int = 100


class Reminder(Struct):
    """Text for the model that is not the user speaking, recorded as
    ``(extension, kind, data)`` with its message. ``role`` ``user`` is
    context that rides in the user's turn (Claude Code's
    ``<system-reminder>``; ``origin`` ``memory`` for recalled memory);
    ``system`` is a harness note in its own system turn after it. The
    reminders of one round are recorded user ones first, then by ``order``
    (adapters join adjacent system turns, so ``order`` places a note inside
    the joined one)."""

    text: str
    kind: str = "reminder"
    data: Any = None
    origin: Literal["system", "memory"] = "system"
    role: Literal["user", "system"] = "user"
    order: int = 100


class Command(Struct):
    """A slash command front ends offer (:mod:`noeta.core.commands`). Typing
    ``/<name> <args>`` sends ``template`` with ``{args}`` replaced as the
    message, or, when ``template`` is empty, the text the extension's
    ``run_command`` returns."""

    name: str
    description: str
    template: str = ""
    #: What to type after the name (``<file> [line]``), shown next to it.
    argument_hint: str = ""


@dataclass(frozen=True)
class CommandContext:
    """Where a command is listed or run: the front end's task (``None``
    before it has one) and working directory."""

    cwd: str
    task_id: str | None = None


@dataclass
class TaskView:
    """What an extension sees: the folded task state (read only) and a way to
    record its own entries. Records made during a hook are appended by the
    runner right after the hook returns."""

    state: TaskState
    #: Call the model outside the step (retried, not recorded as a step).
    complete: Callable[[ModelRequest], Awaitable[ModelResponse]] | None = None
    #: The task's execution environment (files, commands), when the runner
    #: has one.
    env: ExecEnv | None = None
    #: Post to this task's own inbox: ``await post(body, dedupe_key=None)``.
    #: For input that must arrive as inbox items (a notice that opens a
    #: turn, a cancel); idempotent with a ``dedupe_key``.
    post: Callable[..., Awaitable[None]] | None = None
    pending: list[Event] = field(default_factory=list)
    #: The task's extensions, in the agent's order (``residents``).
    extensions: Sequence[Extension] = ()
    #: The model the task runs on (its config's, else its agent's).
    model: str = ""
    #: Every agent this worker can run, by name (a sub-agent roster reads
    #: their descriptions here).
    agents: Mapping[str, Agent] = field(default_factory=dict)
    #: The agent's tool allowlist (``None`` = every tool offered).
    allowed_tools: Sequence[str] | None = None

    @property
    def task_id(self) -> str:
        return self.state.task_id

    def ext_state(self, name: str) -> Any:
        return self.state.ext.get(name)

    def prompt(self, name: str, default: str) -> str:
        """The prompt text ``name`` for this task (``noeta.core.prompts``)."""
        return self.state.prompt(name, default)

    def record(
        self, extension: str, kind: str, data: Any = None, *, message: Message | None = None
    ) -> None:
        """Record an entry; with ``message`` the core also appends that
        message to the conversation history."""
        self.pending.append(
            ExtensionRecorded(extension=extension, kind=kind, data=data, message=message)
        )

    def notify(self, text: str) -> None:
        """Tell the person ``text``; front ends show it, the model never sees
        it (a ``core`` / ``notice`` record)."""
        self.record("core", "notice", {"text": text})

    def find_tool(self, name: str) -> Tool | None:
        """The tool named ``name`` that this task offers (the first extension
        offering it wins, as for the model), or ``None``. How one plugin uses
        another's tool without importing it: model-visible tool names are a
        stable contract."""
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return None
        for ext in self.extensions:
            for tool in ext.tools(self):
                if tool.name == name:
                    return tool
        return None

    def compacted(self, event: Compacted) -> None:
        """Record a compaction (the compaction extension's one core event)."""
        self.pending.append(event)

    async def residents(self) -> list[Message]:
        """Every extension's ``resident`` messages, in extension order: what
        a compaction puts right after its summary (``Compacted.kept``). An
        extension that fails is logged and skipped."""
        out: list[Message] = []
        for ext in self.extensions:
            try:
                out.extend(await ext.resident(self))
            except Exception:
                log.exception("extension %s resident failed", ext.name)
        return out


GatePhase = Literal["model", "user"]


class Extension:
    name: str = ""

    def initial_state(self) -> Any:
        return None

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        """Fold one of this extension's own records into its state."""
        return state

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return ()

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """Reminders to record now. Called before every model call, after
        every ``before_model``, on the state those left (a compaction made
        in this round included); the core records them and puts them in
        this request, after the recorded messages."""
        return ()

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        """Config entries every child task this task spawns starts with,
        laid over the config the spawning tool gave it."""
        return {}

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        return ()

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        """The slash commands this extension offers to a front end."""
        return ()

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        """The message text for one of this extension's commands whose
        ``template`` is empty."""
        raise NotImplementedError(f"{self.name} has no command {name!r}")

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """Content that must sit right after a compaction's summary (a
        listing, project instructions, an index, the environment block). The
        compaction extension calls this (through ``TaskView.residents``) while
        compacting and records the result in ``Compacted.kept``, so the
        request sent right after the compaction is the one rebuilt from the
        log. Records made here (without a message) are appended before the
        ``Compacted``."""
        return ()

    async def on_start(self, view: TaskView) -> None:
        """Called each time a runner picks the task up, before the first step."""

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        return request

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """``None`` = no opinion. Every extension is asked; any ``Deny`` wins.
        A ``Rewrite`` comes next (the rewritten call is then checked again, so
        an approval is asked about the real call), then the first ``Ask``."""
        return None

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        """Observe each appended event (metrics, audit, UI bridges). Records
        made here are appended right after."""

    async def on_context_overflow(self, view: TaskView) -> bool:
        """The model refused the request as too long. Return ``True`` after
        making room (recording a compaction); the call is then retried.
        When no extension does, the turn fails with ``context_overflow``."""
        return False


class ToolSet(Extension):
    """Wraps a plain list of tools as an extension."""

    def __init__(self, tools: Sequence[Tool], name: str = "tools") -> None:
        self.name = name
        self._tools = list(tools)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools
