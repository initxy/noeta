"""Claude Code's quiet-stretch nudge: after several steps in a row that only
called tools, with no text for the user since their last message, the model
is told once (in a system turn) to keep the user posted when there is
something worth saying."""

from __future__ import annotations

from collections.abc import Sequence

from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.types import Message

#: Tool-only steps in a row that earn the nudge (Claude Code's count).
QUIET_STEPS = 5
#: Its place in a joined system turn: before the budget note.
ORDER = 60

NUDGE = (
    "The user has heard nothing from you for a while. When you have something worth "
    "telling them, such as a finding or a change of plan, give them a short update."
)


def quiet_steps(messages: Sequence[Message], nudge: str) -> int:
    """Tool-only steps since the user last spoke or the model last wrote
    text; ``-1`` when this quiet stretch was already nudged."""
    n = 0
    for m in reversed(messages):
        if m.role == "system" and nudge in m.text:
            return -1
        if m.role == "assistant":
            if m.text.strip() or not m.tool_uses:
                break
            n += 1
        elif m.role == "user" and m.origin in (None, "human") and m.text:
            break
    return n


class QuietNudge(Extension):
    """``steps`` — tool-only steps in a row before the nudge."""

    name = "quiet_nudge"

    def __init__(self, *, steps: int = QUIET_STEPS) -> None:
        self.steps = steps

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        text = view.prompt("reminder/quiet_nudge", NUDGE)
        if quiet_steps(view.state.messages, text) < self.steps:
            return ()
        return (Reminder(text=text, kind="nudge", role="system", order=ORDER),)
