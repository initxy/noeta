"""Claude Code's empty-reply nudge: a reply that ends with no text and no
tool call (reasoning alone counts as nothing) is not kept, and the model is
asked to go on, once per step. Asked already, with no reply and no new
message since, the turn ends without an answer, as in Claude Code; so does
an empty reply after a tool that answers for the turn
(``Tool.silent_reply_after``). The nudge is plain text in the user's turn,
as Claude Code sends it. Without this plugin the first empty reply ends the
turn."""

from __future__ import annotations

from noeta.core.extension import Extension, TaskView, offer_tools
from noeta.core.tool import tool_flags
from noeta.core.types import Message, ModelResponse, ToolResult, user
from noeta.toolkit.empty_reply import EMPTY_REPLY_NUDGE


class EmptyReply(Extension):
    name = "empty_reply"

    async def on_empty_reply(self, view: TaskView, response: ModelResponse) -> Message | None:
        if view.state.empty_nudged or answered_by_tool(view):
            return None
        return user(EMPTY_REPLY_NUDGE, origin="inline")


def answered_by_tool(view: TaskView) -> bool:
    """A call since the person's last message whose tool says no reply need
    follow it (``Tool.silent_reply_after``): the turn's answer went through
    the tool, so an empty reply ends the turn without the nudge."""
    offer = offer_tools(view.extensions, view, view.allowed_tools, view.denied_tools)
    modes = {t.name: tool_flags(t).silent_reply_after for t in offer.tools}
    if not any(modes.values()):
        return False
    failed: dict[str, bool] = {}
    for m in reversed(view.state.messages):
        results = [b for b in m.content if isinstance(b, ToolResult)]
        if m.from_person and not results:
            return False
        failed.update((b.call_id, b.is_error) for b in results)
        for use in m.tool_uses if m.role == "assistant" else ():
            mode = modes.get(use.name)
            if mode == "call" or (mode == "success" and failed.get(use.id) is False):
                return True
    return False
