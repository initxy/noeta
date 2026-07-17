"""Channel read tools (host side).

`channel_read_history` / `channel_read_topic`: the channel context injected
into a topic goal is a truncated window; older main-stream messages and the
full transcripts of historical topics are fetched on demand through these two
tools. The tools are wrapped into DecoratedTool via
``noeta.tools.decorator.tool`` and mixed into ``Options.allowed_tools`` (side
by side with builtin tool name strings, the noeta 0.2.4 mixed-entry
contract); pure host-side sqlite reads, no container dependency (the same
host-side policy as the memory tools).

Ownership resolution: ``ToolContext.metadata["task_id"]`` → session_tasks →
channel session → channel (ChannelService.resolve_channel_for_task). Only the
owning channel is readable; calls from tasks that are not channel topics
return a failure hint (the tools are registered globally, and the
descriptions state they only apply inside channel topics).

ChannelService is created later than AgentService in the lifespan, so this
module holds a zero-arg getter (it resolves to the instance only after
attach_channel_service) and fetches it at invoke time.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.decorator import DecoratedTool, tool

_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "before_seq": {
            "type": "integer",
            "description": "Only return messages before this seq (paging backwards); omit = the latest page",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of messages to return (default 50, max 100)",
        },
    },
}

_TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topic_id": {
            "type": "string",
            "description": "Topic ID (the historical-topic index in the channel context carries topic_id)",
        }
    },
    "required": ["topic_id"],
}

_NOT_IN_CHANNEL = "The current task does not belong to any channel topic; this tool is only available inside channel topics."


def build_channel_tools(
    get_channel_service: Callable[[], Optional[Any]],
) -> tuple[DecoratedTool, DecoratedTool]:
    def _resolve(ctx: ToolContext) -> tuple[Optional[Any], Optional[dict]]:
        svc = get_channel_service()
        if svc is None:
            return None, None
        task_id = str(ctx.metadata.get("task_id") or "")
        channel = svc.resolve_channel_for_task(task_id) if task_id else None
        return svc, channel

    @tool(
        name="channel_read_history",
        version="1",
        risk_level="low",
        input_schema=_HISTORY_SCHEMA,
        description=(
            "Read the historical messages of the current channel's main stream "
            "(chat between members, ascending by seq). The recent messages "
            "injected into the topic goal are a truncated window; use this tool "
            "to page back when earlier discussion is needed (use the smallest "
            "seq among the returned messages as the next call's before_seq). "
            "Only available inside channel topics."
        ),
    )
    def channel_read_history(arguments: dict, ctx: ToolContext) -> ToolResult:
        svc, channel = _resolve(ctx)
        if svc is None or channel is None:
            return ToolResult(success=False, output=_NOT_IN_CHANNEL)
        before_seq = arguments.get("before_seq")
        limit = arguments.get("limit") or 50
        try:
            msgs = svc.read_history(
                channel,
                int(before_seq) if before_seq is not None else None,
                int(limit),
            )
        except (TypeError, ValueError):
            return ToolResult(success=False, output="before_seq / limit must be integers")
        if not msgs:
            return ToolResult(success=True, output="(no earlier messages)")
        lines = [
            f"[seq={m['seq']}] [{m['author']}] {m['text']}"
            + (" (this message spawned a topic)" if m["topic_id"] else "")
            for m in msgs
        ]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            summary=f"read {len(msgs)} historical messages from channel #{channel['name']}",
        )

    @tool(
        name="channel_read_topic",
        version="1",
        risk_level="low",
        input_schema=_TOPIC_SCHEMA,
        description=(
            "Read the full conversation transcript of a historical topic in the "
            "current channel (user messages + agent replies). The "
            "historical-topic index in the channel context has only a one-line "
            "preview; use this tool to fetch the full text when the topic's "
            "conclusions and details are needed. Only available inside channel "
            "topics."
        ),
    )
    def channel_read_topic(arguments: dict, ctx: ToolContext) -> ToolResult:
        svc, channel = _resolve(ctx)
        if svc is None or channel is None:
            return ToolResult(success=False, output=_NOT_IN_CHANNEL)
        topic_id = str(arguments.get("topic_id") or "")
        transcript = svc.read_topic(channel, topic_id) if topic_id else None
        if transcript is None:
            return ToolResult(
                success=False, output=f"topic not found or not in this channel: {topic_id}"
            )
        if not transcript:
            return ToolResult(success=True, output="(this topic has no conversation content yet)")
        return ToolResult(
            success=True,
            output=transcript,
            summary=f"read the full conversation of topic {topic_id}",
        )

    return channel_read_history, channel_read_topic


__all__ = ["build_channel_tools"]
