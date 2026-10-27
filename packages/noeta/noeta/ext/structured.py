"""Structured output: each turn's final answer parsed as JSON and recorded,
so a host reads the value (dict stays dict) and not the transcript.

0.6 parsed the answer with ``json.loads`` when ``output_schema`` was set and
fell back to the raw text when it was not JSON — a turn never fails over a
malformed answer — and kept that value on every turn, including a turn that
parks waiting for the next message (ADR-045).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import msgspec

from noeta.events import ExtensionRecorded, ModelReplied, Recorded
from noeta.ext.base import Extension, TaskView
from noeta.types import ModelRequest

NAME = "structured_output"


def parse_answer(text: str) -> tuple[Any, bool]:
    """``(value, parsed)``: the JSON value of ``text``, or the text itself."""
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return text, False


def render_answer(value: Any) -> str:
    """A structured answer as text: a string verbatim, anything else as
    sorted, indented JSON (never a Python repr), as 0.6 rendered it."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except (TypeError, ValueError):
        return str(value)


def structured_answer(records: Iterable[Recorded]) -> Any:
    """The latest turn's answer value from a task's events (``None`` when no
    turn has finished yet)."""
    value: Any = None
    for rec in records:
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == NAME and ev.kind == "answer":
            value = ev.data.get("value") if isinstance(ev.data, dict) else None
    return value


class StructuredOutput(Extension):
    """Records ``{"value", "parsed", "reply_seq"}`` (kind ``answer``) each
    time a reply closes a turn; ``structured_answer(events)`` reads the
    latest. The folded state is that latest record.

    Constructor: ``output_schema`` — the JSON Schema to request when the
    agent sets none itself (``Agent.output_schema`` already reaches the
    provider; this lets a host attach structured output as an extension).
    Install it on agents that ask for structured output (0.6 parsed only
    when a schema was set); it records on every turn it sees.
    """

    name = NAME

    def __init__(self, output_schema: dict[str, Any] | None = None) -> None:
        self.output_schema = output_schema

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data if kind == "answer" else state

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if request.output_schema is None and self.output_schema is not None:
            request = msgspec.structs.replace(request, output_schema=self.output_schema)
        return request

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if not isinstance(ev, ModelReplied) or ev.response.message.tool_uses:
            return
        if ev.response.stop_reason == "max_tokens":
            return  # a continuation follows; the answer is not complete yet
        self._record(view, recorded.seq)

    async def on_start(self, view: TaskView) -> None:
        # A crash between a turn's last reply and its record.
        st = view.state
        if st.turn_open or st.calls or not st.messages or not st.message_seqs:
            return
        last, seq = st.messages[-1], st.message_seqs[-1]
        if last.role != "assistant" or last.tool_uses or seq == 0:
            return
        recorded = view.ext_state(self.name)
        if isinstance(recorded, dict) and recorded.get("reply_seq") == seq:
            return
        self._record(view, seq)

    def _record(self, view: TaskView, seq: int) -> None:
        value, parsed = parse_answer(view.state.last_answer)
        view.record(self.name, "answer", {"value": value, "parsed": parsed, "reply_seq": seq})
