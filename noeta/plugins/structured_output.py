"""Structured output, as Claude Code gives it: a task whose config sets
``output_schema`` (``noeta -p --json-schema``, the headless client's
``jsonSchema``, an HTTP request's config, a workflow's ``agent({schema})``)
is offered a ``StructuredOutput`` tool whose input schema is that schema.
The model hands its final answer in by calling it: a call that fits the
schema is recorded (``noeta.toolkit.structured_output``) and ends the turn
with the value as its answer; one that does not gets an error result naming
what is wrong. A reply that ends the turn without an accepted call since
the person's last message is answered once with a reminder to call it; a
turn whose calls keep being refused fails after ``max_retries`` of them.

Everything is decided from the task's log (the recorded calls and
messages), so a task that moves to another worker or restarts decides the
same. The provider's own ``output_schema`` (constrained decoding) stays for
side requests (a title, a recall); the main conversation never uses it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from noeta.core import records
from noeta.core.errors import TurnFailed
from noeta.core.events import ExtensionRecorded, ModelReplied, Recorded, Say
from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ToolResult, user
from noeta.toolkit.json_schema import schema_errors
from noeta.toolkit.structured_output import (
    ANSWER_KIND,
    PLUGIN,
    RETRIES_REASON,
    SCHEMA_KEY,
    TOOL,
    structured_answer,
)

__all__ = ["ENFORCE_TAG", "StructuredOutput", "StructuredOutputTool", "structured_answer"]

NAME = PLUGIN

DESCRIPTION = (
    "Hand in your final response in the structured format this request asks for. Call it "
    "once, when your work is done: the request is complete only after this call, and its "
    "input is what is returned.")
ACCEPTED = "Answer recorded."
MISMATCH = "Not accepted, the input breaks the required schema: {problems}"
#: Claude Code's tag for the reminder (kept verbatim: it marks the message).
ENFORCE_TAG = "[structured-output-enforce]"
ENFORCE = (f"{ENFORCE_TAG} Your answer still has to go through the {TOOL} tool; this "
           "request is not finished until you make that call.")
#: Refused calls in one turn before it fails (Claude Code's
#: ``MAX_STRUCTURED_OUTPUT_RETRIES`` default).
MAX_RETRIES = 5
RETRIES_DETAIL = "{tool} was called {n} times without a valid answer"
RETRIES_LAST = "; the last error: {error}"


class StructuredOutputTool:
    """``StructuredOutput`` for one schema."""

    name = TOOL
    description = DESCRIPTION
    read_only = True
    clearable = False
    #: Pure: a call a crash cut short runs again.
    retry_safe = True
    #: Offered with a schema whatever an agent's ``tools:`` list says.
    past_agent_lists = True

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self.schema = dict(schema)
        self.input_schema = self.schema

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        problems = schema_errors(input, self.schema)
        if problems:
            return text_output(MISMATCH.format(problems="; ".join(problems)), is_error=True)
        ctx.record(NAME, ANSWER_KIND, {"value": input, "call_id": ctx.call_id})
        return ToolOutput(content=text_output(ACCEPTED).content,
                          end_turn=json.dumps(input, ensure_ascii=False,
                                              separators=(",", ":")))


@lru_cache(maxsize=64)
def _tool(schema_json: str) -> StructuredOutputTool:
    return StructuredOutputTool(json.loads(schema_json))


def _prompt(m: Message) -> bool:
    return m.from_person and not any(isinstance(b, ToolResult) for b in m.content)


def _since_prompt(messages: Sequence[Message]) -> list[Message]:
    """The messages after the person's last one."""
    for i in range(len(messages) - 1, -1, -1):
        if _prompt(messages[i]):
            return list(messages[i + 1:])
    return list(messages)


def _attempts(messages: Sequence[Message]) -> tuple[int, str]:
    """``StructuredOutput`` calls since the person's last message and the
    last accepted call (Claude Code counts the calls of the run, which an
    accepted one ends), and the last refused one's error."""
    calls: set[str] = set()
    last_error = ""
    for m in _since_prompt(messages):
        if m.role == "assistant":
            calls.update(u.id for u in m.tool_uses if u.name == TOOL)
        for b in m.content:
            if isinstance(b, ToolResult) and b.call_id in calls:
                if b.is_error:
                    last_error = b.text
                else:
                    calls.clear()
                    last_error = ""
    return len(calls), last_error


def _nudged(messages: Sequence[Message], text: str) -> bool:
    return any(m.role == "user" and not _prompt(m)
               and (m.text == text or m.text.startswith(ENFORCE_TAG))
               for m in _since_prompt(messages))


class StructuredOutput(Extension):
    """Constructor: ``output_schema`` — the schema when neither the task's
    config nor its agent (``Agent.output_schema``) sets one;
    ``max_retries`` — refused calls in a turn before it fails. Every agent
    built from agent files runs it; it does nothing for a task without a
    schema."""

    name = NAME

    def __init__(self, output_schema: dict[str, Any] | None = None, *,
                 max_retries: int = MAX_RETRIES) -> None:
        self.output_schema = output_schema
        self.max_retries = max_retries

    def schema(self, view: TaskView) -> dict[str, Any] | None:
        """The answer's schema for this task, if it has one."""
        config = view.state.config
        schema = config.get(SCHEMA_KEY) if isinstance(config, Mapping) else None
        if schema is None:
            agent = view.agents.get(view.state.agent)
            schema = getattr(agent, "output_schema", None)
        if schema is None:
            schema = self.output_schema
        return dict(schema) if isinstance(schema, Mapping) else None

    def tools(self, view: TaskView) -> Sequence[Tool]:
        schema = self.schema(view)
        if schema is None:
            return ()
        return (_tool(json.dumps(schema, sort_keys=True)),)

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data if kind == ANSWER_KIND else state

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if self.schema(view) is None:
            return request
        attempts, error = _attempts(view.state.messages)
        if attempts >= self.max_retries:
            detail = RETRIES_DETAIL.format(tool=TOOL, n=attempts)
            if error:
                detail += RETRIES_LAST.format(error=error)
            raise TurnFailed(RETRIES_REASON, detail)
        return request

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ModelReplied):
            response = ev.response
            if response.message.tool_uses or response.stop_reason == "max_tokens" \
                    or not response.message.text.strip():
                return  # a call, a continuation, or the core's empty-reply nudge
            self._enforce(view, recorded.seq)
        elif isinstance(ev, ExtensionRecorded) and ev.extension == "core" \
                and ev.kind == "turn_done" and isinstance(ev.data, dict) \
                and ev.data.get("reason") == records.EMPTY_REPLY:
            self._enforce(view, recorded.seq)

    async def on_start(self, view: TaskView) -> None:
        # A crash between a turn's last reply and its reminder: the reply is
        # still the log's last event (otherwise the turn went on, or the
        # task waited and whatever came since is newer than it).
        st = view.state
        if st.turn_open or st.calls or not st.messages or not st.message_seqs \
                or st.message_seqs[-1] != st.last_seq:
            return
        last = st.messages[-1]
        if last.role == "assistant" and not last.tool_uses and last.text.strip():
            self._enforce(view, st.message_seqs[-1])

    def _enforce(self, view: TaskView, seq: int) -> None:
        """Remind the model once per message of the person's when its turn
        ended without an accepted ``StructuredOutput`` call."""
        if self.schema(view) is None or view.state.turn_open:
            return
        messages = view.state.messages
        if not any(m.from_person for m in messages):
            return
        text = view.prompt("structured_output/enforce", ENFORCE)
        if _nudged(messages, text) or self._answered(view):
            return
        view.record(NAME, "enforced", {"reply_seq": seq})
        view.enqueue(Say(message=user(text, origin="inline"), when="now"),
                     dedupe_key=f"{NAME}:enforce:{view.task_id}:{seq}")

    def _answered(self, view: TaskView) -> bool:
        """An accepted call since the person's last message."""
        accepted = view.ext_state(self.name)
        call_id = accepted.get("call_id") if isinstance(accepted, dict) else None
        if not call_id:
            return False
        return any(any(u.id == call_id for u in m.tool_uses)
                   for m in _since_prompt(view.state.messages) if m.role == "assistant")
