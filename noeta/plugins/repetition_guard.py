"""The repetition (loop) guard: a call identical to the ones right before it
(same tool, same arguments), ``threshold`` times in a row counting itself,
is asked about or refused. Claude Code has no such guard, so the built-in
agents keep it off until a host sets ``[plugins.repetition_guard]``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from noeta.core.extension import Ask, Deny, Extension, Gate, TaskView
from noeta.core.types import Message, ToolUse

__all__ = ["RepetitionAction", "RepetitionGuard", "RepetitionPolicy", "recent_calls",
           "repetition_verdict"]

NAME = "repetition_guard"

RepetitionAction = Literal["require_approval", "deny"]
_ACTIONS = ("require_approval", "deny")


@dataclass(frozen=True)
class RepetitionPolicy:
    """``threshold`` consecutive identical calls (counting the proposed one)
    trip ``action``; ``threshold <= 0`` is off. ``window`` bounds how many
    recent calls are looked at."""

    threshold: int = 3
    action: RepetitionAction = "require_approval"
    window: int = 8


def _key(use: ToolUse) -> tuple[str, str]:
    return use.name, json.dumps(use.input, sort_keys=True, separators=(",", ":"), default=str)


def recent_calls(messages: Sequence[Message], before: str, window: int) -> list[ToolUse]:
    """The last ``window`` tool calls of the conversation before call
    ``before`` (calls of the same reply that precede it count)."""
    out: list[ToolUse] = []
    found = False
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for use in reversed(m.tool_uses):
            if not found:
                if use.id == before:
                    found = True
                continue
            out.append(use)
            if len(out) >= window:
                return list(reversed(out))
        found = True  # an earlier reply: everything in it precedes the call
    return list(reversed(out))


def repetition_verdict(p: RepetitionPolicy, call: ToolUse,
                       history: Sequence[ToolUse]) -> tuple[RepetitionAction, str] | None:
    if p.threshold <= 0:
        return None
    key = _key(call)
    run = 0
    for prior in reversed(history):
        if _key(prior) != key:
            break
        run += 1
    consecutive = run + 1
    if consecutive < p.threshold:
        return None
    reason = (
        f"tool {call.name!r} called with identical arguments "
        f"{consecutive} times in a row (repetition threshold "
        f"{p.threshold}); suspected loop"
    )
    return p.action, reason


class RepetitionGuard(Extension):
    """Arguments: ``threshold`` (default 3; ``<= 0`` is off), ``action``
    (``require_approval``, the default, asks the person; ``deny`` refuses)
    and ``window`` (the recent calls looked at, default 8). Stateless: it
    reads the conversation, so any worker sees the same run of calls."""

    name = NAME

    def __init__(self, *, threshold: int = 3, action: str = "require_approval",
                 window: int = 8) -> None:
        if action not in _ACTIONS:
            raise ValueError(f"repetition_guard action must be one of {_ACTIONS}; got {action!r}")
        self.policy = RepetitionPolicy(threshold, cast(RepetitionAction, action), window)

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        if self.policy.threshold <= 0:
            return None
        history = recent_calls(view.state.messages, call.id, self.policy.window)
        verdict = repetition_verdict(self.policy, call, history)
        if verdict is None:
            return None
        if verdict[0] == "deny":
            return Deny(reason=verdict[1])
        return Ask(prompt=verdict[1], detail={"by": NAME, "tool": call.name}, firm=True)
