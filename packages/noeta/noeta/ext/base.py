"""The one extension interface.

Everything beyond the core loop (permissions, subtasks, compaction, memory,
skills, MCP, background shells) is an ``Extension``. Every method is
optional. An extension keeps durable state by recording
``ExtensionRecorded`` entries (``view.record``) and folding them in
``reduce``; the core never grows an event type for an extension.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Union

from noeta.events import Compacted, Event, ExtensionRecorded, Recorded
from noeta.execenv.base import ExecEnv
from noeta.tools.base import Tool, ToolOutput
from noeta.types import Message, ModelRequest, ModelResponse, Struct, ToolUse

if TYPE_CHECKING:
    from noeta.loop.state import TaskState


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
    """System prompt text an extension contributes. ``cache`` marks the end
    of a stable prefix. Text that must sit in the conversation instead is
    recorded as a message (``TaskView.record(..., message=...)``) so the
    request prefix stays byte-stable."""

    text: str
    cache: bool = False
    order: int = 100


class Command(Struct):
    """A slash command front ends offer. Running it posts ``template`` (with
    ``{args}`` filled in) as a message, or calls the extension's ``command``
    method when ``template`` is empty."""

    name: str
    description: str
    template: str = ""


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
    pending: list[Event] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return self.state.task_id

    def ext_state(self, name: str) -> Any:
        return self.state.ext.get(name)

    def record(
        self, extension: str, kind: str, data: Any = None, *, message: Message | None = None
    ) -> None:
        """Record an entry; with ``message`` the core also appends that
        message to the conversation history."""
        self.pending.append(
            ExtensionRecorded(extension=extension, kind=kind, data=data, message=message)
        )

    def compacted(self, event: Compacted) -> None:
        """Record a compaction (the compaction extension's one core event)."""
        self.pending.append(event)


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

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        return ()

    def commands(self) -> Sequence[Command]:
        return ()

    async def on_start(self, view: TaskView) -> None:
        """Called each time a runner picks the task up, before the first step."""

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        return request

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        """``None`` = no opinion. Every extension is asked; any ``Deny`` wins,
        then the first ``Ask``, then the first ``Rewrite``."""
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
