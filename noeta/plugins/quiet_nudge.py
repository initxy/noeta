"""Claude Code's quiet-stretch nudge: after several steps in a row that only
called tools, with no text for the user since their last message (or their
answer to a question), the model is told (in a system turn) to keep the user
posted when there is something worth saying. A long quiet stretch is nudged
again after as many more such steps, up to three times. As in Claude Code,
only the models with the capability get it (:func:`gets_nudge`)."""

from __future__ import annotations

from collections.abc import Sequence

from noeta.core import models
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.types import Message

#: Tool-only steps in a row that earn the nudge (Claude Code's count).
QUIET_STEPS = 5
#: Nudges one quiet stretch gets at most (Claude Code's).
MAX_NUDGES = 3
#: Its place in a joined system turn: before the budget note.
ORDER = 60
#: Calls the person answers: the quiet stretch starts again after one.
ASKS_USER = frozenset({"AskUserQuestion", "ExitPlanMode"})

#: Claude Code 2.1.312's models with the quiet-stretch reminder
#: (``silent_turn_reminder``: Sonnet 5.5 by its catalog row, Opus 5.5 and
#: Fable 5.1 by their prompt bundles), matched in the id as it matches its
#: Claude models (``claude-opus-5-5[1m]``, ``us.anthropic.claude-opus-5-5``).
NUDGE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1",
                "claude-mythos-5-1")


def gets_nudge(model: str) -> bool:
    """Whether ``model`` gets the quiet-stretch nudge (an alias resolves
    first; any other model, Haiku 5.5 included, gets none)."""
    real = models.resolve_alias(model).lower()
    return any(known in real for known in NUDGE_MODELS)


NUDGE = (
    "The user has heard nothing from you for a while. When you have something worth "
    "telling them, such as a finding or a change of plan, give them a short update."
)


def quiet_steps(messages: Sequence[Message], nudge: str) -> tuple[int, int]:
    """In the current quiet stretch (since the user last spoke or the model
    last wrote text): the tool-only steps since it began or since its last
    nudge, and how many nudges it has had."""
    since = nudges = 0
    for m in reversed(messages):
        if m.role == "system" and nudge in m.text:
            nudges += 1
        elif m.role == "assistant":
            if m.text.strip() or not m.tool_uses \
                    or any(u.name in ASKS_USER for u in m.tool_uses):
                break
            if not nudges:
                since += 1
        elif m.from_person and m.text:
            break
    return since, nudges


class QuietNudge(Extension):
    """``steps`` — tool-only steps in a row before each nudge; ``most`` —
    nudges per quiet stretch."""

    name = "quiet_nudge"

    def __init__(self, *, steps: int = QUIET_STEPS, most: int = MAX_NUDGES) -> None:
        self.steps = steps
        self.most = most

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        if not gets_nudge(str(view.state.config.get("model") or view.model or "")):
            return ()
        text = view.prompt("reminder/quiet_nudge", NUDGE)
        since, nudges = quiet_steps(view.state.messages, text)
        if since < self.steps or nudges >= self.most:
            return ()
        return (Reminder(text=text, kind="nudge", role="system", order=ORDER),)
