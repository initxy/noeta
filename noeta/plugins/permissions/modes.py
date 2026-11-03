"""Switching permission modes, and the auto-mode notes a mode owes the
next request. Plan mode's switches, plan file and notes are the plan-mode
plugin's."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.core.extension import Reminder, TaskView
from noeta.core.types import ToolResult
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.state import NAME, auto_active, state_of

#: Place in the joined system turn (``Reminder.order``): before the token
#: budget, after the plan-mode notes.
ORDER_AUTO = 70


def switch(view: TaskView, mode: Any, *, current: str, disable_bypass: bool) -> None:
    """Record a switch from ``current`` to ``mode``, when it is one. A switch
    into plan mode is the plan-mode plugin's, never this one."""
    if not isinstance(mode, str) or mode not in p.PERMISSION_MODES or mode == "plan":
        return
    if mode == "bypassPermissions" and disable_bypass:
        view.notify("bypassPermissions is disabled here")
        return
    if mode == current:
        return
    view.record(NAME, "mode", {"mode": mode})


async def mode_reminders(view: TaskView, mode: str) -> Sequence[Reminder]:
    """The auto-mode notes owed to this request (recorded as sent)."""
    st = state_of(view)
    out: list[Reminder] = []
    sent: list[str] = []
    auto_noted = st.auto_noted
    active = auto_active(mode, st.plan)
    if "auto_exited" in st.due and not active:
        if view.state.parent_id is None:
            out.append(Reminder(text=w.AUTO_MODE_EXITED, kind="auto_exited", role="system",
                                order=ORDER_AUTO))
            sent.append("auto_exited")
        auto_noted = False
    # Claude Code sends the same note in bypassPermissions mode; noeta does
    # not: with it the eval model worked around a deny rule through the
    # shell more often (parity/deviations.md).
    if mode == "auto" and not auto_noted and _shell_first(view) \
            and _guidance_due(view, st.guidance_after):
        out.append(Reminder(text=w.AUTO_MODE_GUIDANCE, kind="auto_guidance", role="system",
                            order=ORDER_AUTO))
        sent.append("auto_guidance")
        auto_noted = True
    # Owed notes a later switch made moot: the exit from auto mode when it
    # is back on (or in a sub-agent).
    stale = [k for k in st.due if k not in sent and k == "auto_exited"]
    if sent or stale or auto_noted != st.auto_noted:
        view.record(NAME, "noted", {"kinds": [*sent, *stale], "auto_noted": auto_noted})
    return out


def _shell_first(view: TaskView) -> bool:
    """Whether the auto-mode note fits the agent: it has the shell and a file
    tool that writes (Claude Code sends none to an agent without them, such
    as Explore)."""
    return view.find_tool("Bash") is not None and any(
        view.find_tool(name) is not None for name in ("Edit", "Write"))


def _guidance_due(view: TaskView, after: int) -> bool:
    """The auto-mode note goes with the main conversation's first request or
    the next one with input newer than seq ``after`` (a compaction), and a
    sub-agent's second (as Claude Code sends them)."""
    if view.state.parent_id is not None:
        return any(m.role == "assistant" for m in view.state.history)
    return _new_input(view, after)


def _new_input(view: TaskView, after: int) -> bool:
    """Whether a prompt or a tool result came in after log seq ``after``."""
    return any(seq > after and m.role == "user" and (
        m.origin in (None, "human") or any(isinstance(b, ToolResult) for b in m.content))
        for seq, m in zip(view.state.message_seqs, view.state.messages, strict=False))
