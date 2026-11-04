"""The tool interface and what a tool sees while it runs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import msgspec

from noeta.core.execenv import ExecEnv
from noeta.core.records import CoreNotices
from noeta.core.types import (
    Display,
    Effort,
    Image,
    ModelRequest,
    ModelResponse,
    Struct,
    Text,
    ToolReference,
)

_log = logging.getLogger("noeta.core.tool")


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


@dataclass(eq=False)
class KeptClaim:
    """A task's claim kept by its worker (``ToolContext.keep``): while one
    is held, the task's runner keeps its claim when the task waits, renewing
    the lease and taking the task's inbox itself. :meth:`release` gives it
    back (once). Once the claim is gone (lost, the task ended, the worker
    stopping) it is :attr:`lost`, and so is what it kept: :meth:`on_lost`
    registers the stop."""

    lost: bool = False
    _on_lost: list[Callable[[], None]] = field(default_factory=list, repr=False)
    _release: Callable[[KeptClaim], None] = field(default=lambda _kept: None, repr=False)

    def on_lost(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` once the claim is lost (at once if it is). The
        returned call takes it back."""
        if self.lost:
            callback()
        else:
            self._on_lost.append(callback)

        def forget() -> None:
            if callback in self._on_lost:
                self._on_lost.remove(callback)

        return forget

    def release(self) -> None:
        self._on_lost.clear()
        self._release(self)

    def mark_lost(self) -> None:
        if self.lost:
            return
        self.lost = True
        callbacks, self._on_lost = self._on_lost, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 — one failing stop leaves the others to run
                _log.exception("a lost-claim callback failed")


@dataclass
class ToolContext(CoreNotices):
    task_id: str
    call_id: str
    env: ExecEnv
    #: Folded state of every extension, by extension name (read only).
    state: Mapping[str, Any]
    #: Record ``(extension, kind, data)``; the runner appends it as an
    #: ``ExtensionRecorded`` just before the call's ``ToolFinished``. Raises
    #: ``ValueError`` for a ``core`` record of a kind the core does not know.
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
    #: finishing), or with ``task_id=`` to any other task (a child whose job
    #: is stopped; another conversation ``SendMessage`` writes to, as Claude
    #: Code messages its other open sessions); an unknown task raises
    #: ``UnknownTask``. Wakes the task if it is waiting; idempotent with a
    #: ``dedupe_key=``.
    post: Callable[..., Awaitable[None]] | None = None
    #: The root task's id.
    root_id: str = ""
    #: ``keep()`` keeps this task's claim with this worker, over the task's
    #: waits too, until the returned :class:`KeptClaim` is released: for what
    #: lives only in this process (a background job), so no other worker
    #: runs the task meanwhile and its next turn finds the job here.
    keep: Callable[[], KeptClaim] | None = None
    #: ``await tasks(parent_id=..., status=..., limit=...)``: rows of the
    #: store's task table (``Store.tasks``), e.g. to
    #: list other tasks a message could go to.
    tasks: Callable[..., Awaitable[list[Any]]] | None = None
    #: ``await task(task_id)``: the store's row of one task, ``None`` when
    #: there is none.
    task: Callable[[str], Awaitable[Any]] | None = None
    #: ``await read_log(task_id, after=0)``: another task's records
    #: (``TaskView.read_log``); an unknown task reads as empty.
    read_log: Callable[..., Awaitable[Sequence[Any]]] | None = None
    #: The model made this call (the runner's calls), so what earlier calls
    #: returned is in its conversation; ``False`` when an extension runs a
    #: tool itself.
    by_model: bool = False
    #: A person allowed this call at its approval prompt (rather than no
    #: extension asking).
    approved: bool = False
    #: A permission layer allowed this call by its settings (a mode, an
    #: allow rule) and nobody was asked (``Allow.outright``). ``False`` where
    #: no permission layer judges calls.
    allowed_outright: bool = False
    #: The model the task runs on (``""`` when not known).
    model: str = ""
    #: The identity line the task's requests open with (``TaskView.identity``).
    identity: str = ""
    #: The task's effort (``TaskView.effort``).
    effort: Effort | None = None
    #: ``prompt(name, default)``: the task's text for a named prompt
    #: (``noeta.core.prompts``).
    prompt: Callable[[str, str], str] = field(default=lambda _name, default: default)

    #: ``await count_tokens(text)``: the provider's exact token count of one
    #: user message holding ``text``; ``None`` when it cannot count.
    count_tokens: Callable[[str], Awaitable[int | None]] | None = None
    #: ``await complete(request)``: call the model through the task's
    #: providers (retried, not recorded as a step), as ``TaskView.complete``;
    #: ``None`` where no runner runs the tool.
    complete: Callable[[ModelRequest], Awaitable[ModelResponse]] | None = None
    #: ``progress(text)``: show front ends watching live what the call is
    #: doing (its latest output, replacing the one before). Never recorded
    #: nor sent to the model; best effort. ``None`` where nobody watches.
    progress: Callable[[str], None] | None = None

    @property
    def idempotency_key(self) -> str:
        """``<task id>:<call id>``: the same for every run of this call on
        any worker (a re-run after a crash, a late run on a worker that
        froze). Pass it to an outside system that can drop a repeat (an HTTP
        ``Idempotency-Key``, MCP's ``_meta``), or look the outcome up by it
        in ``recover``."""
        return f"{self.task_id}:{self.call_id}"


class Tool(Protocol):
    """A tool the model can call.

    Optional members, read with their default when a tool leaves them out:

    * ``deferred: bool = False`` — named to the model by name only
      (``TaskView.deferred_tool_names``) and sent with ``defer_loading``; the
      ``tool_search`` plugin withholds it until the model loads it.
    * ``search_hint: str = ""`` — a few words a ``ToolSearch`` keyword query
      also matches (Claude Code's ``searchHint``); never sent to the model.
    * ``loaded_description: str`` — the description sent instead of
      ``description`` while the tool goes with ``defer_loading`` (the model
      loaded it through ``ToolSearch``), as Claude Code gives such a tool a
      longer one.
    * ``listed_last: bool = False`` — sent after the other tools (keeps a
      changing set of tools at the end of the cached prefix).
    * ``run_last: bool = False`` — runs after the reply's other calls
      (delegation).
    * ``past_agent_lists: bool = False`` — offered whenever its extension
      offers it, whatever the agent's tool allow and deny lists say: the task
      cannot finish without it (the answer tool a requested answer schema
      adds, as Claude Code adds it to every agent's tools).
    * ``async resume(input, ctx, outcome) -> ToolOutput`` — required of a
      tool that parks: ``outcome`` is the ``Answer`` for a question, or
      ``{child_id: ChildEnded}`` for children; ``None`` means the wait was
      withdrawn (the turn was stopped).
    * ``retry_safe: bool = False`` — running it again changes nothing
      outside the call (a read): a call that a crash left unfinished runs
      again (up to :data:`MAX_ATTEMPTS` starts) instead of being answered
      "outcome unknown".
    * ``async recover(input, ctx) -> ToolOutput | None`` — for a call that a
      crash left unfinished: its result, looked up by
      ``ctx.idempotency_key`` (or got by running it again where the outside
      system drops a repeat of that key); ``None`` when the outcome cannot
      be told (then a ``retry_safe`` tool runs again, any other is answered
      "outcome unknown"). Asked before ``retry_safe``.
    * ``silent_reply_after: Literal["call", "success"] | None = None`` —
      a reply with nothing in it after a call of this tool since the
      person's last message ends the turn instead of being asked to go on:
      after any call (``"call"``: the answer tool, whose call is the
      answer) or a successful one (``"success"``: a tool whose result is
      meant to end the turn).
    * ``async rule_inputs(input, view) -> Sequence[(input, allow_too)]`` —
      other inputs the permission rules also judge the call as (a ``Skill``
      call under its skill's former name): a deny or ask rule matching any
      applies, an allow rule (or a hook ``if`` whose hook allows) only one
      marked ``allow_too``.
    * ``unchecked_input: bool = False`` — its input goes to it unchecked
      (an MCP server's tool, a host function: Claude Code passes those on
      as sent); every other tool's input is checked against its schema
      before any gate (``noeta.core.tool_input``).
    * ``safe_while_planning: bool = False`` — not read-only, yet changes no
      file and nothing outside the task: it talks to the person, keeps the
      task's own lists, or starts or answers a sub-agent (which is gated on
      its own). Plan mode lets it run without asking.
    * ``max_result_chars: int | None`` — the runner saves a text result
      longer than this (at most :data:`RESULT_CEILING_CHARS`, the default) to
      a file and sends a preview (``noeta.core.large_output``); ``None``
      exempts a tool that bounds its own result (``Read``).
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


#: Claude Code's ceiling on any tool's inline text result, in characters.
RESULT_CEILING_CHARS = 50_000
#: Claude Code's budget for the text results of one reply's calls together.
REPLY_RESULTS_BUDGET_CHARS = 200_000


#: Starts of a ``retry_safe`` call before a crash during it is answered
#: "outcome unknown" anyway (a call that kills its worker is not run forever).
MAX_ATTEMPTS = 3


def tool_flag(tool: Tool,
              name: Literal["deferred", "listed_last", "run_last", "retry_safe",
                            "past_agent_lists", "unchecked_input",
                            "safe_while_planning"]) -> bool:
    """One of ``tool``'s optional flags (see :class:`Tool`); ``False`` when
    the tool leaves it out."""
    return bool(getattr(tool, name, False))
