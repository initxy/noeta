"""What the ACP adapter (:mod:`noeta.host.acp`) sends and reads: recorded
replies and tool results as ACP session updates, an editor's prompt blocks as
a message, parked questions as text, and one prompt's live output reconciled
with its recorded reply."""

from __future__ import annotations

import asyncio
import base64
import difflib
import os
import re
from collections.abc import Iterable
from typing import Any

import acp
from acp import schema

from noeta.core.events import ExtensionRecorded, ModelReplied, Recorded, ToolFinished, Waiting
from noeta.core.types import Delta, Image, Message, Text, ToolUse
from noeta.host.feed import tool_title
from noeta.toolkit.plugin_records import PERMISSIONS

#: Tool name → ACP tool kind (what an editor shows next to the call).
TOOL_KINDS: dict[str, str] = {
    "Read": "read", "Edit": "edit", "Write": "edit", "NotebookEdit": "edit", "Bash": "execute",
    "TaskStop": "execute", "Monitor": "execute",
    "ToolSearch": "search",
    "WebFetch": "fetch", "WebSearch": "fetch", "Task": "think", "Agent": "think",
}
#: Characters of a tool result shown in the editor.
RESULT_PREVIEW_CHARS = 4000


def _path(call: ToolUse) -> str | None:
    path = call.input.get("file_path") or call.input.get("notebook_path") \
        or call.input.get("path")
    return path if isinstance(path, str) else None


def _locations(call: ToolUse) -> list[schema.ToolCallLocation] | None:
    path = _path(call)
    return [schema.ToolCallLocation(path=path)] if path is not None else None


def change_content(call: ToolUse) -> list[Any] | None:
    """The diff an editor can show before a call runs: the text an Edit
    replaces and what replaces it."""
    path, old, new = _path(call), call.input.get("old_string"), call.input.get("new_string")
    if call.name == "Edit" and path and isinstance(old, str) and isinstance(new, str):
        return [acp.tool_diff_content(path, new, old)]
    return None


_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _old_text(new: str, diff: str) -> str | None:
    """The text a unified ``diff`` turned into ``new``, rebuilt by running it
    backwards; ``None`` unless diffing the result with ``new`` gives exactly
    ``diff`` back (a line without a final newline makes the diff ambiguous)."""
    after = new.splitlines(keepends=True)
    lines = diff.splitlines(keepends=True)
    if not lines:
        return new
    if len(lines) < 3 or not lines[0].startswith("---") or not lines[1].startswith("+++"):
        return None
    old: list[str] = []
    at = 0
    for line in lines[2:]:
        hunk = _HUNK.match(line)
        if hunk is not None:
            start, length = int(hunk[1]), int(hunk[2] or 1)
            to = start if length == 0 else start - 1
            if to < at:
                return None
            old += after[at:to]
            at = to
        elif line[:1] in (" ", "+"):
            at += 1
            if line[0] == " ":
                old.append(line[1:])
        elif line[:1] == "-":
            old.append(line[1:])
        else:
            return None
    old += after[at:]
    text = "".join(old)
    again = difflib.unified_diff(text.splitlines(keepends=True), after)
    return text if "".join(list(again)[2:]) == "".join(lines[2:]) else None


def _result_content(call: ToolUse, meta: dict[str, Any]) -> list[Any] | None:
    """A finished file change as a diff of the file (or notebook cell)."""
    path = _path(call)
    if path is None:
        return None
    if call.name == "Edit":
        return change_content(call)
    if call.name == "Write" and isinstance(content := call.input.get("content"), str):
        if meta.get("created"):
            return [acp.tool_diff_content(path, content, None)]
        diff = meta.get("diff")
        old = _old_text(content, diff) if isinstance(diff, str) else None
        return [acp.tool_diff_content(path, content, old)] if old is not None else None
    if call.name == "NotebookEdit" and "old_source" in meta:
        new = "" if meta.get("mode") == "delete" else str(call.input.get("new_source") or "")
        old_source = meta["old_source"]
        return [acp.tool_diff_content(path, new, old_source if isinstance(old_source, str)
                                      else None)]
    return None


def start_updates(reply: ModelReplied, *, streamed_text: bool) -> list[Any]:
    out: list[Any] = []
    message = reply.response.message
    if message.text and not streamed_text:
        out.append(acp.update_agent_message_text(message.text))
    for call in message.tool_uses:
        out.append(acp.start_tool_call(
            call.id, tool_title(call), kind=TOOL_KINDS.get(call.name, "other"),  # type: ignore[arg-type]
            status="pending", content=change_content(call), locations=_locations(call),
            raw_input=call.input))
    return out


def finish_update(ev: ToolFinished, call: ToolUse | None = None) -> Any:
    """A tool's result; a file change the editor can show as a diff when
    ``call`` (the call it answers) is known."""
    r = ev.result
    text = r.text
    if len(text) > RESULT_PREVIEW_CHARS:
        text = text[:RESULT_PREVIEW_CHARS] + "\n…"
    content: list[Any] = []
    diff = ev.meta.get("diff")
    changed = _result_content(call, ev.meta) if call is not None and not r.is_error else None
    if changed:
        content += changed
    elif isinstance(diff, str) and diff:
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


_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*\S)")
_HEADING = re.compile(r"#{1,6}\s+(.*?)(?:\s+#+)?\s*$")


def plan_entries(text: str) -> list[schema.PlanEntry]:
    """A plan file's text as ACP plan entries: its list items (a checked
    box is done), else its headings, else its first line."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    items = [(m[2], m[1] in ("x", "X")) for m in map(_ITEM.match, lines) if m]
    if not items:
        items = [(m[1], False) for m in map(_HEADING.match, lines) if m and m[1]]
    if not items:
        items = [(line.strip(), False) for line in lines if line.strip()][:1]
    return [acp.plan_entry(content, status="completed" if done else "pending")
            for content, done in items]


class SessionLog:
    """What one session's updates need from records already seen: the calls
    not yet answered, by id, and plan mode's plan file with its text as the
    calls that wrote it left it."""

    def __init__(self, cwd: str = "") -> None:
        self.cwd = cwd
        self.calls: dict[str, ToolUse] = {}
        #: The newest record taken in.
        self.seq = 0
        self.plan_path: str | None = None
        self.plan_text: str | None = None
        #: The plan text changed since :meth:`plan_update` last said so.
        self.plan_changed = False

    def note(self, rec: Recorded) -> ToolUse | None:
        """Take in ``rec`` (once; an older record is skipped); for a tool
        result, the call it answers."""
        if rec.seq <= self.seq:
            return None
        self.seq = rec.seq
        ev = rec.event
        if isinstance(ev, ModelReplied):
            self.calls.update((c.id, c) for c in ev.response.message.tool_uses)
        elif isinstance(ev, ToolFinished):
            call = self.calls.pop(ev.result.call_id, None)
            if call is not None and not ev.result.is_error:
                self._wrote(call)
            return call
        elif isinstance(ev, ExtensionRecorded) and ev.extension == PERMISSIONS \
                and isinstance(ev.data, dict) and isinstance(ev.data.get("plan"), dict):
            path = ev.data["plan"].get("path")
            if isinstance(path, str) and path and self._full(path) != self.plan_path:
                self.plan_path, self.plan_text = self._full(path), None
        return None

    def _full(self, path: str) -> str:
        return os.path.normpath(os.path.join(self.cwd, path))

    def _wrote(self, call: ToolUse) -> None:
        path = _path(call)
        if self.plan_path is None or path is None or self._full(path) != self.plan_path:
            return
        text = self.plan_text
        if call.name == "Write":
            text = call.input.get("content") if isinstance(call.input.get("content"), str) \
                else None
        elif call.name == "Edit" and text is not None:
            old, new = call.input.get("old_string"), call.input.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or old not in text:
                text = None  # changed some way these calls do not show
            else:
                text = text.replace(old, new, -1 if call.input.get("replace_all") else 1)
        if text is not None and text != self.plan_text:
            self.plan_changed = True
        self.plan_text = text

    def plan_update(self, *, always: bool = False) -> Any | None:
        """The plan as an ACP ``plan`` update when it changed (or, with
        ``always``, whenever it is known)."""
        changed, self.plan_changed = self.plan_changed, False
        if self.plan_text is None or not (changed or always):
            return None
        return acp.update_plan(plan_entries(self.plan_text))


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
        #: Calls whose result was shown: progress arriving late (it travels
        #: apart from the log) never turns one back to in progress.
        self.finished: set[str] = set()

    def on_delta(self, task_id: str, delta: Delta) -> None:
        if task_id != self.session_id or not delta.text or self.done:
            return
        if delta.kind == "tool":  # a running call's latest output
            if delta.call_id in self.finished:
                return
            self.out.put_nowait(acp.update_tool_call(
                delta.call_id, status="in_progress",
                content=[acp.tool_content(acp.text_block(delta.text))]))
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
        if delta.kind not in ("text", "thinking"):
            return  # a kind this version does not know
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
