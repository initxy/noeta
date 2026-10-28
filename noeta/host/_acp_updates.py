"""What the ACP adapter (:mod:`noeta.host.acp`) sends and reads: recorded
replies and tool results as ACP session updates, an editor's prompt blocks as
a message, parked questions as text, and one prompt's live output reconciled
with its recorded reply."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from typing import Any

import acp
from acp import schema

from noeta.core.events import ModelReplied, ToolFinished, Waiting
from noeta.core.types import Delta, Image, Message, Text, ToolUse
from noeta.host.feed import tool_title

#: Tool name → ACP tool kind (what an editor shows next to the call).
TOOL_KINDS: dict[str, str] = {
    "Read": "read", "Edit": "edit", "Write": "edit", "Bash": "execute",
    "TaskStop": "execute", "Monitor": "execute",
    "ToolSearch": "search",
    "WebFetch": "fetch", "WebSearch": "fetch", "Task": "think", "Agent": "think",
}
#: Characters of a tool result shown in the editor.
RESULT_PREVIEW_CHARS = 4000


def _locations(call: ToolUse) -> list[schema.ToolCallLocation] | None:
    path = call.input.get("file_path") or call.input.get("path")
    return [schema.ToolCallLocation(path=str(path))] if isinstance(path, str) else None


def start_updates(reply: ModelReplied, *, streamed_text: bool) -> list[Any]:
    out: list[Any] = []
    message = reply.response.message
    if message.text and not streamed_text:
        out.append(acp.update_agent_message_text(message.text))
    for call in message.tool_uses:
        out.append(acp.start_tool_call(
            call.id, tool_title(call), kind=TOOL_KINDS.get(call.name, "other"),  # type: ignore[arg-type]
            status="pending", locations=_locations(call), raw_input=call.input))
    return out


def finish_update(ev: ToolFinished) -> Any:
    r = ev.result
    text = r.text
    if len(text) > RESULT_PREVIEW_CHARS:
        text = text[:RESULT_PREVIEW_CHARS] + "\n…"
    content: list[Any] = []
    diff = ev.meta.get("diff")
    if isinstance(diff, str) and diff:
        content.append(acp.tool_content(acp.text_block(diff)))
    elif text:
        content.append(acp.tool_content(acp.text_block(text)))
    return acp.update_tool_call(r.call_id, status="failed" if r.is_error else "completed",
                                content=content or None, raw_output=text)


def prompt_message(blocks: Iterable[Any]) -> Message:
    content: list[Any] = []
    for b in blocks:
        kind = getattr(b, "type", "")
        if kind == "text":
            content.append(Text(text=b.text))
        elif kind == "image":
            content.append(Image(media_type=b.mime_type, data=base64.b64decode(b.data)))
        elif kind == "resource_link":
            content.append(Text(text=f"@{b.uri}"))
        elif kind == "resource":
            res = b.resource
            body = getattr(res, "text", None)
            content.append(Text(text=f'<file uri="{res.uri}">\n{body}\n</file>' if body
                                else f"@{res.uri}"))
    return Message(role="user", content=content or [Text(text="")], origin="human")


def question_ids(waiting: Waiting) -> list[str]:
    """The calls a ``Waiting`` asks the user about (its detail is keyed by
    call id, each entry with the parked call's ``on``)."""
    return [k for k, v in waiting.detail.items()
            if isinstance(v, dict) and v.get("on") == "question"]


def question_text(detail: dict[str, Any], only: dict[str, Any] | None = None) -> str:
    """A parked question as plain text: each question (or ``only`` that
    one) and its options."""
    lines: list[str] = []
    for q in [only] if only is not None else detail.get("questions") or []:
        lines.append(str(q.get("question") or ""))
        for opt in q.get("options") or []:
            desc = opt.get("description")
            lines.append(f"  - {opt.get('label', '')}" + (f": {desc}" if desc else ""))
    return "\n".join(lines) or str(detail.get("prompt") or "")


class PromptTurn:
    """One prompt's view of the task: which inbox item started it, and the
    live output that belongs to it."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.out: asyncio.Queue[Any] = asyncio.Queue()
        #: The inbox seq of this prompt's message (or answer).
        self.seq = -1
        #: The log seq of the ``InputTaken`` that took it; ``None`` until seen.
        self.input_seq: int | None = None
        self.called = 0
        self.done = False
        self._early: list[Delta] = []
        #: Per model call (its ``ModelCalled`` seq): the live attempt and the
        #: text streamed for it.
        self._attempt: dict[int, int] = {}
        self._text: dict[int, str] = {}
        #: Calls whose failed attempt already showed text (the retry's text
        #: is set apart from it).
        self._shown: set[int] = set()

    def on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id != self.session_id or not delta.text or self.done:
            return
        if self.input_seq is None:
            self._early.append(delta)  # sorted out once the turn's start is known
            return
        self._forward(delta)

    def start(self, input_seq: int) -> None:
        self.input_seq = input_seq
        early, self._early = self._early, []
        for d in early:
            self._forward(d)

    def _forward(self, delta: Delta) -> None:
        assert self.input_seq is not None
        if delta.call_seq and delta.call_seq <= self.input_seq:
            return  # an earlier turn's call
        key = delta.call_seq
        live = self._attempt.get(key, 1)
        if delta.attempt < live:
            return  # a failed attempt's tail
        if delta.attempt > live:
            self.retried(delta.attempt - 1, key)
        if delta.kind == "text":
            if not self._text.get(key) and key in self._shown:
                self._shown.discard(key)
                self.out.put_nowait(acp.update_agent_message_text("\n\n"))
            self._text[key] = self._text.get(key, "") + delta.text
            self.out.put_nowait(acp.update_agent_message_text(delta.text))
        else:
            self.out.put_nowait(acp.update_agent_thought_text(delta.text))

    def retried(self, failed_attempt: int, key: int | None = None) -> None:
        """Attempt ``failed_attempt`` of a call failed: its fragments are
        dropped and the next attempt's text is reconciled on its own."""
        key = self.called if key is None else key
        if failed_attempt + 1 <= self._attempt.get(key, 1):
            return
        self._attempt[key] = failed_attempt + 1
        if self._text.get(key):
            self._shown.add(key)
        self._text[key] = ""

    def unsent_text(self, full: str) -> str:
        """What of a recorded reply's text the editor has not been sent."""
        sent = self._text.pop(self.called, "")
        if not full or sent == full:
            return ""
        if full.startswith(sent):
            return full[len(sent):]
        return f"\n\n{full}"
