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

from noeta.core.deferred import deferred_names
from noeta.core.events import Compacted, Event, ExtensionRecorded, Recorded, Say, TaskCreated
from noeta.core.execenv import ExecEnv
from noeta.core.tool import Tool, ToolOutput, notice_data
from noeta.core.types import Message, ModelRequest, ModelResponse, Struct, ToolUse, user

if TYPE_CHECKING:
    from noeta.core.agent import Agent
    from noeta.core.loop.state import TaskState

log = logging.getLogger("noeta.plugins")


class Allow(Struct, tag="allow", tag_field="gate"):
    """No objection. With ``approve`` the extension also vouches for the
    call (a PreToolUse hook that allows it): other extensions' asks are
    satisfied, except a ``firm`` one; a deny still wins."""

    approve: bool = False


class Deny(Struct, tag="deny", tag_field="gate"):
    reason: str = ""


class Ask(Struct, tag="ask", tag_field="gate"):
    """Park the call until someone answers. ``prompt`` is what a front end
    shows; ``detail`` rides along to it. A ``firm`` ask (an ask rule, a
    hook asking) is not satisfied by another extension's approving
    ``Allow``; it still reaches ``resolve_ask``, whose resolvers decide."""

    prompt: str = ""
    detail: dict[str, Any] = {}
    firm: bool = False


class Rewrite(Struct, tag="rewrite", tag_field="gate"):
    """Run the call with different input (or a different tool)."""

    input: dict[str, Any]
    name: str | None = None


Gate = Union[Allow, Deny, Ask, Rewrite]


class ContextPart(Struct):
    """System prompt text an extension contributes: it continues the agent's
    prompt, after a newline, in ``order``, inside the prompt's one cached
    block. With ``before``, it goes ahead of the prompt's first line equal
    to it instead (a section placed before a heading), followed by a blank
    line; a prompt without that line gets it at the end. Text that must sit
    in the conversation instead is recorded as a message
    (``TaskView.record(..., message=...)``) so the request prefix stays
    byte-stable."""

    text: str
    order: int = 100
    before: str = ""


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
    ``run_command`` returns. A ``local`` command's text is shown to the
    person instead and never reaches the model (Claude Code's local
    commands)."""

    name: str
    description: str
    template: str = ""
    #: What to type after the name (``<file> [line]``), shown next to it.
    argument_hint: str = ""
    local: bool = False


@dataclass(frozen=True)
class CommandContext:
    """Where a command is listed or run: the front end's task (``None``
    before it has one) and working directory."""

    cwd: str
    task_id: str | None = None


class StagedChild(Struct):
    """A child task ``TaskView.spawn`` staged; the runner creates it."""

    task_id: str
    created: TaskCreated
    queue: str
    first: Say


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
    #: Child tasks staged by ``spawn``, created with ``pending``.
    creates: list[StagedChild] = field(default_factory=list)
    #: ``await count_tokens(text)``: the provider's exact token count of one
    #: user message holding ``text`` (``None`` when it cannot count).
    count_tokens: Callable[[str], Awaitable[int | None]] | None = None
    #: The task's extensions, in the agent's order (``residents``).
    extensions: Sequence[Extension] = ()
    #: The model the task runs on (its config's, else its agent's).
    model: str = ""
    #: Every agent this worker can run, by name (a sub-agent roster reads
    #: their descriptions here).
    agents: Mapping[str, Agent] = field(default_factory=dict)
    #: The agent's tool allowlist (``None`` = every tool offered).
    allowed_tools: Sequence[str] | None = None
    #: Tool names the agent never offers.
    denied_tools: Sequence[str] = ()

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

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None:
        """Tell the person ``text``; front ends show it, the model never sees
        it (a ``core`` / ``notice`` record). ``kind`` and ``data`` carry the
        same news as structured data for a front end that renders it
        (``text`` stays the plain fallback)."""
        self.record("core", "notice", notice_data(text, kind, data))

    def set_timer(self, name: str, at: float, text: str = "") -> None:
        """Wake the task at ``at`` (epoch seconds) once it is idle, with
        ``text`` as the next message (``TimerFired(name)`` either way); a
        timer of the same name is replaced. Durable: any worker fires it."""
        self.record("core", "timer", {"name": name, "at": at, "text": text})

    def clear_timer(self, name: str) -> None:
        self.record("core", "timer_cleared", {"name": name})

    def spawn(self, agent: str, prompt: str | Message, *, config: Mapping[str, Any] | None = None,
              queue: str = "default", key: str = "") -> str:
        """Stage a child task outside a tool call (a background
        orchestration): created with the records this hook leaves, and
        tracked like any child (its ``ChildEnded`` arrives in the inbox).
        ``key`` names the child; the same key gives the same child id."""
        child_id = f"{self.task_id}.{key or len(self.creates) + 1}"
        message = user(prompt) if isinstance(prompt, str) else prompt
        child_config = dict(config or {})
        for ext in self.extensions:
            child_config.update(ext.inherit(self.state))
        self.creates.append(StagedChild(
            task_id=child_id,
            created=TaskCreated(agent=agent, config=child_config, parent_id=self.task_id,
                                root_id=self.state.root_id or self.task_id),
            queue=queue, first=Say(message=message)))
        self.record("core", "spawned", {"children": [
            {"id": child_id, "call_id": "", "background": True}]})
        return child_id

    def find_tool(self, name: str) -> Tool | None:
        """The tool named ``name`` that this task offers (the first extension
        offering it wins, as for the model), or ``None``. How one plugin uses
        another's tool without importing it: model-visible tool names are a
        stable contract."""
        if (self.allowed_tools is not None and name not in self.allowed_tools) \
                or name in self.denied_tools:
            return None
        for ext in self.extensions:
            for tool in ext.tools(self):
                if tool.name == name:
                    return tool
        return None

    def deferred_tool_names(self) -> list[str]:
        """The names of the deferred tools this task offers, sorted (loaded
        or not): the list a message announces so the model can load them
        with ``ToolSearch`` (``noeta.core.deferred``)."""
        allowed = self.allowed_tools
        return deferred_names(t for ext in self.extensions for t in ext.tools(self)
                              if allowed is None or t.name in allowed)

    def compacted(self, event: Compacted) -> None:
        """Record a compaction (the compaction extension's one core event)."""
        self.pending.append(event)

    async def residents(self, *, lead: bool = False) -> list[Message]:
        """Every extension's ``resident`` messages (``lead_resident`` with
        ``lead``), in extension order: what a compaction rebuilds after the
        kept messages (ahead of its summary). An extension that fails is
        logged and skipped."""
        out: list[Message] = []
        for ext in self.extensions:
            try:
                out.extend(await (ext.lead_resident(self) if lead else ext.resident(self)))
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
        """Content a compaction re-attaches after the messages it keeps (a
        listing, the environment block). The compaction extension calls this
        (through ``TaskView.residents``) while compacting and records the
        result in ``Compacted.after``, so the request sent right after the
        compaction is the one rebuilt from the log. Records made here
        (without a message) are appended before the ``Compacted``."""
        return ()

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        """Content a compaction rebuilds ahead of its summary (project
        instructions, the memory index), recorded in ``Compacted.lead``;
        otherwise as ``resident``."""
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

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        """The call is about to park on ``asks`` (nobody denied it). Return
        ``Allow`` or ``Deny`` to answer in a person's place (a classifier, a
        PermissionRequest hook, an approval callback), a ``Rewrite`` to run
        it with other input (gated again), an ``Ask`` to put to the person
        in place of the asks, or ``None``. Asked in extension
        order; the first answer wins. A call can be gated again (after a
        restart), so an answer that cost something is recorded and reused."""
        return None

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        """Observe each appended event (metrics, audit, UI bridges). Records
        made here are appended right after."""

    async def before_compact(self, view: TaskView, trigger: Literal["manual", "auto"],
                             instructions: str) -> str | Deny | None:
        """A compaction is about to summarize the conversation (``manual``:
        a ``/compact``; ``auto``: the context filled up). ``instructions``
        is what the person asked the summary to focus on. Return text to add
        to those instructions, a ``Deny`` to skip this compaction, or
        ``None``. The compacting extension asks every extension in order."""
        return None

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
