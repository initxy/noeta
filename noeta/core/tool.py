"""The tool interface and what a tool sees while it runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import msgspec

from noeta.core.execenv import ExecEnv
from noeta.core.holds import Release
from noeta.core.records import CoreNotices
from noeta.core.types import Display, Image, Struct, Text, ToolReference


class Park(Struct):
    """Returned by a tool that must wait before it has a result: for the
    user's answer to a question (``on="question"``, keyed by the call id) or
    for child tasks it spawned (``on="children"``, keyed by their ids). The
    task waits; when the condition is met the runner calls the tool's
    ``resume`` with what arrived."""

    on: Literal["question", "children"]
    keys: list[str] = msgspec.field(default_factory=list)
    detail: dict[str, Any] = msgspec.field(default_factory=dict)


class ToolOutput(Struct):
    content: list[Text | Image | ToolReference] = msgspec.field(default_factory=list)
    is_error: bool = False
    #: Recorded with the result for front ends and extensions; never sent to
    #: the model.
    meta: dict[str, Any] = msgspec.field(default_factory=dict)
    display: Display | None = None
    #: Set instead of a result when the call must wait (see ``Park``).
    park: Park | None = None
    #: End the turn once this reply's results land, with this text as the
    #: turn's answer: no further model call (a sub-agent's hand-back).
    end_turn: str | None = None
    #: Send the result as a list of content blocks even when it is plain
    #: text (``ToolResult.block_list``).
    block_list: bool = False


def text_output(text: str, *, is_error: bool = False, **meta: Any) -> ToolOutput:
    return ToolOutput(content=[Text(text=text)], is_error=is_error, meta=meta)


@dataclass
class ToolContext(CoreNotices):
    task_id: str
    call_id: str
    env: ExecEnv
    #: Folded state of every extension, by extension name (read only).
    state: Mapping[str, Any]
    #: Record ``(extension, kind, data)``; the runner appends it as an
    #: ``ExtensionRecorded`` just before the call's ``ToolFinished``.
    record: Callable[[str, str, Any], None]
    #: Directory for full outputs too large to return inline, or ``None``.
    spill_dir: str | None = None
    #: Stage a child task: ``spawn(agent, prompt, config=..., background=...,
    #: history_from=..., history_through=...)`` returns its id (``history_*``:
    #: the child continues that task's conversation, see ``TaskCreated``). The
    #: child is created atomically with this call's record, so a retried call
    #: never makes a second child.
    spawn: Callable[..., str] | None = None
    #: Post to this task's own inbox from outside a step (a background job
    #: finishing), or with ``task_id=`` to any other task in the store (the
    #: root once the spawner has ended; another conversation ``SendMessage``
    #: writes to, as Claude Code messages its other open sessions). Wakes the
    #: task if it is waiting; idempotent with a ``dedupe_key=``.
    post: Callable[..., Awaitable[None]] | None = None
    #: The root task's id (background jobs notify the spawner, then the root).
    root_id: str = ""
    #: ``await hold()`` keeps this task tree with this worker until the
    #: returned release is awaited (``Runtime.hold``): for what lives only in
    #: this process, e.g. a background job. The release's ``tree`` is the
    #: hold: its worker and epoch, and ``on_lost`` to stop with it.
    hold: Callable[[], Awaitable[Release]] | None = None
    #: ``await tasks(parent_id=..., status=..., limit=...)``: rows of the
    #: store's task table (``Store.tasks``), e.g. to list other tasks a
    #: message could go to.
    tasks: Callable[..., Awaitable[list[Any]]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    #: The model made this call (the runner's calls), so what earlier calls
    #: returned is in its conversation; ``False`` when an extension runs a
    #: tool itself.
    by_model: bool = False
    #: A person allowed this call at its approval prompt (rather than no
    #: extension asking).
    approved: bool = False
    #: ``prompt(name, default)``: the task's text for a named prompt
    #: (``noeta.core.prompts``).
    prompt: Callable[[str, str], str] = field(default=lambda _name, default: default)

    #: ``await count_tokens(text)``: the provider's exact token count of one
    #: user message holding ``text``; ``None`` when it cannot count.
    count_tokens: Callable[[str], Awaitable[int | None]] | None = None


class Tool(Protocol):
    """A tool the model can call.

    Optional members, read with their default when a tool leaves them out:

    * ``deferred: bool = False`` — named to the model and sent only once it
      loads the tool with ``ToolSearch`` (``noeta.core.deferred``).
    * ``listed_last: bool = False`` — sent after the other tools (keeps a
      changing set of tools at the end of the cached prefix).
    * ``run_last: bool = False`` — runs after the reply's other calls
      (delegation).
    * ``async resume(input, ctx, outcome) -> ToolOutput`` — required of a
      tool that parks: ``outcome`` is the ``Answer`` for a question, or
      ``{child_id: ChildEnded}`` for children; ``None`` means the wait was
      withdrawn (the turn was stopped).
    """

    name: str
    #: The model-facing description: the single source of the tool's
    #: semantics. Never restated in the system prompt.
    description: str
    input_schema: dict[str, Any]
    #: Only reads and holds no shared state: may run alongside other
    #: read-only calls of the same reply.
    read_only: bool
    #: Its result can be fetched again by calling it again, so an old result
    #: may be cleared from the context.
    clearable: bool

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput: ...


def tool_flag(tool: Tool, name: Literal["deferred", "listed_last", "run_last"]) -> bool:
    """One of ``tool``'s optional flags (see :class:`Tool`); ``False`` when
    the tool leaves it out."""
    return bool(getattr(tool, name, False))
