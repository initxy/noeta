"""The ``empty_reply`` plugin's nudge, for the Anthropic adapter that places
it: Claude Code adds it to the user turn before the step's notes, so the
adapter tells it from other inline text by these exact words."""

from __future__ import annotations

__all__ = ["EMPTY_REPLY_NUDGE"]

#: Asked once per step when a reply ends with no text and no tool call
#: (Claude Code 2.1.312's text).
EMPTY_REPLY_NUDGE = (
    "[Your previous response had no visible output. Please continue and "
    "produce a user-visible response.]"
)
