"""The ``ultrathink`` keyword, as Claude Code reads it: a person's prompt
with the word gets a note asking for deeper reasoning, and, when a person
follows the task live, the rest of that turn runs at ``xhigh`` effort
(unless the effort is that high already). Read from the conversation on
every model call, so any worker decides alike. The ``environment`` plugin
applies it."""

from __future__ import annotations

import re
from collections.abc import Sequence

import msgspec

from noeta.core import models
from noeta.core.extension import Reminder, TaskView
from noeta.core.types import Message, ModelRequest, ToolResult
from noeta.toolkit.interactive import interactive

__all__ = ["KEYWORD", "ULTRATHINK_NOTE", "ultrathink_effort", "ultrathink_reminders"]

KEYWORD = re.compile(r"\bultrathink\b", re.IGNORECASE)
ULTRATHINK_NOTE = (
    'The user\'s message contains the keyword "ultrathink": they ask for deeper reasoning this '
    "turn. Think as thoroughly as the task calls for.")
#: The effort the turn is raised to, and the levels already at or above it.
RAISED = "xhigh"
_HIGH_ENOUGH = frozenset({"xhigh", "max"})


def _prompt(m: Message) -> bool:
    return m.from_person and m.origin == "human" \
        and not any(isinstance(b, ToolResult) for b in m.content)


def ultrathink_reminders(view: TaskView) -> Sequence[Reminder]:
    """The note, with the first model call that sees such a prompt (the
    prompts after the model's last reply): once per prompt, so not again
    when that call is made again or the turn ended before a reply (the note
    already follows the prompt)."""
    for m in reversed(view.state.messages):
        if m.role == "assistant" or (m.text == ULTRATHINK_NOTE and not _prompt(m)):
            return ()
        if _prompt(m) and KEYWORD.search(m.text):
            return (Reminder(text=ULTRATHINK_NOTE, kind="ultrathink", data={}),)
    return ()


def ultrathink_effort(view: TaskView, request: ModelRequest) -> ModelRequest:
    """``request`` at ``xhigh`` effort when a prompt of the current turn has
    the keyword, a person follows the task (Claude Code raises it only in
    its interactive terminal) and the model is a catalogued reasoning model
    (Claude Code: one that takes an effort level); a prompt sent mid-turn
    raises the rest of the turn."""
    if request.effort in _HIGH_ENOUGH or not interactive(view.state.config):
        return request
    spec = models.find_spec(request.model)
    if spec is None or not spec.is_reasoning:
        return request  # a model not known to take an effort level
    for m in reversed(view.state.messages):
        if m.role == "assistant" and not m.tool_uses:
            break  # the previous turn's last reply
        if _prompt(m) and KEYWORD.search(m.text):
            return msgspec.structs.replace(request, effort=RAISED)
    return request
