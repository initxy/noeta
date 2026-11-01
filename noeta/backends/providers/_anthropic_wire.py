"""Outbound half of the Anthropic adapter: messages as Messages-API turns,
the ``thinking`` parameter, and the prompt-cache breakpoints."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from noeta.backends.providers._common import (
    is_host_injected,
    neutralize_reserved_tags,
    tool_result_images,
    tool_result_text,
)
from noeta.core import models
from noeta.core.runner.texts import EMPTY_REPLY_NUDGE, INTERRUPTED_MARKER, REJECTED_MARKER
from noeta.core.types import (
    Image,
    Message,
    ModelRequest,
    Text,
    Thinking,
    ToolReference,
    ToolResult,
    ToolUse,
)

#: ``Provider.family`` and ``Thinking.family`` for this adapter.
FAMILY = "anthropic"

#: Anthropic's per-request cap on ``cache_control`` breakpoints.
_MAX_CACHE_BREAKPOINTS = 4

#: How a message the person sent during a running turn is shown, inside the
#: user turn that carries the tool results.
MID_TURN_MESSAGE = (
    "While you were working, the user sent this message:\n{text}\n\n"
    "This is how a message sent during a turn reaches you: inside the turn, usually "
    "alongside the next tool result. Address it as you carry on."
)

#: The single ephemeral cache breakpoint marker (default 5-minute TTL).
_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def messages_to_wire(
    messages: Sequence[Message], vision: bool, system_turns: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None], list[list[dict[str, Any]]]]:
    """``messages`` as wire turns, with each message's last wire block at or
    before it (what a ``cache_after`` index stamps) and its own wire blocks
    (what ``cache_blocks`` stamps).

    Host context in the user channel renders as <system-reminder>-wrapped
    text and merges into the adjacent user wire turn. Only pairs touching an
    injected turn merge; plain consecutive user turns keep their 1:1
    rendering. Adjacent system turns join into one, their texts a blank line
    apart. ``system_turns=False`` sends system turns as host text instead."""
    outbound: list[dict[str, Any]] = []
    #: The dicts are shared by reference with ``outbound`` (merging spreads
    #: the same dicts), so stamping later lands on the body.
    last_block: list[dict[str, Any] | None] = []
    blocks_of: list[list[dict[str, Any]]] = []
    prev_injected = False
    for message in messages:
        if message.role == "system" and not system_turns:
            message = Message(role="user", content=message.content, origin="system")
        wire = _message_to_wire(message, vision)
        blocks_of.append(list(wire["content"]))
        injected = is_host_injected(message)
        previous = last_block[-1] if last_block else None
        if (wire["role"] == "system" and wire["content"] and outbound
                and outbound[-1]["role"] == "system"):
            joined = outbound[-1]["content"][-1]
            joined["text"] += "\n\n" + "\n\n".join(b["text"] for b in wire["content"])
            last_block.append(joined)
            continue
        if (message.role == "user" and message.origin == "inline"
                and message.text == EMPTY_REPLY_NUDGE):
            turn = next((t for t in reversed(outbound) if t["role"] != "system"), None)
            if turn is not None and turn["role"] == "user":
                # Claude Code adds the empty-reply nudge to the user turn
                # before the step's notes; the request still ends with them.
                turn["content"] = [*turn["content"], *wire["content"]]
                last_block.append(previous)
                prev_injected = injected
                continue
        last_block.append(wire["content"][-1] if wire["content"] else previous)
        mid_turn = (
            message.role == "user" and message.origin == "human" and outbound
            and outbound[-1]["role"] == "user"
            and any(b.get("type") == "tool_result" for b in outbound[-1]["content"])
            and not any(b.get("type") == "tool_result" for b in wire["content"])
        )
        after_stop = (
            message.role == "user" and message.origin == "human" and outbound
            and outbound[-1]["role"] == "user" and outbound[-1]["content"]
            and outbound[-1]["content"][-1].get("text") in (REJECTED_MARKER,
                                                            INTERRUPTED_MARKER))
        if after_stop:
            # The turn stopped at a declined call or mid-reply: the next
            # prompt joins the marker's turn, unframed (Claude Code).
            outbound[-1]["content"] = [*outbound[-1]["content"], *wire["content"]]
            prev_injected = injected
            continue
        if mid_turn:
            # A message the person sent while the turn ran rides in the
            # tool results' turn, framed as Claude Code frames it.
            for block in wire["content"]:
                if block.get("type") == "text":
                    block["text"] = MID_TURN_MESSAGE.format(text=block["text"])
        # Host text meant to read as part of the person's turn rides in it, plain.
        inline = message.role == "user" and message.origin == "inline"
        # A local command's message is no turn of its own: the next
        # prompt joins it (Claude Code, after /compact).
        after_command = (
            message.role == "user" and outbound and outbound[-1]["role"] == "user"
            and any(b.get("type") == "text"
                    and str(b.get("text", "")).startswith(_COMMAND_TAGS[2])
                    for b in outbound[-1]["content"][-1:]))
        if (
            (injected or prev_injected or mid_turn or inline or after_command)
            and wire["role"] == "user"
            and outbound
            and outbound[-1]["role"] == "user"
        ):
            outbound[-1]["content"] = [*outbound[-1]["content"], *wire["content"]]
        else:
            outbound.append(wire)
        prev_injected = injected
    return outbound, last_block, blocks_of


def thinking_param(request: ModelRequest) -> dict[str, str] | None:
    """``thinking`` → the Messages ``thinking`` parameter.

    ``False`` is an explicit ``{"type": "disabled"}``, ``None`` leaves the
    parameter out. ``True`` asks for ``{"type": "adaptive"}`` only on a model
    catalogued as a reasoning model: on an unknown or non-reasoning model
    (Haiku 4.5 takes only the older budget form) the parameter is left out
    and the vendor default applies, which is what every request carried
    before the field became a bool.
    """
    if request.thinking is None:
        return None
    if not request.thinking:
        return {"type": "disabled"}
    spec = models.find_spec(request.model)
    if spec is not None and spec.is_reasoning:
        return {"type": "adaptive", "display": "updates"}
    return None


def bare_strings(outbound: list[dict[str, Any]]) -> None:
    """A user or system turn of one text block with no breakpoint goes as a
    bare string, as Claude Code sends it."""
    for wire in outbound:
        content = wire["content"]
        if (wire["role"] != "assistant" and len(content) == 1
                and content[0].get("type") == "text" and "cache_control" not in content[0]):
            wire["content"] = content[0]["text"]


def apply_cache_control(
    body: dict[str, Any],
    request: ModelRequest,
    last_block: list[dict[str, Any] | None],
    blocks_of: list[list[dict[str, Any]]] | None = None,
) -> None:
    """Stamp ephemeral prompt-cache breakpoints onto the wire body.

    Anthropic renders the prefix ``tools`` → ``system`` → ``messages`` and a
    breakpoint caches everything before it. Candidates, highest priority
    first, until the vendor cap of :data:`_MAX_CACHE_BREAKPOINTS`:

    * each ``cache_after`` message index, newest first (the composer names
      the last message, as Claude Code does);
    * each ``cache_blocks`` block, newest first;
    * each ``SystemPart`` with ``cache=True``, last first.

    The tools get no mark of their own: the first system block's covers
    them. A block already stamped is not counted twice.
    """
    targets: list[dict[str, Any]] = []
    for index in sorted(set(request.cache_after), reverse=True):
        if 0 <= index < len(last_block):
            block = last_block[index]
            if block is not None:
                targets.append(block)
    for mi, bi in sorted(set(request.cache_blocks), reverse=True):
        if blocks_of is not None and 0 <= mi < len(blocks_of) and \
                0 <= bi < len(blocks_of[mi]):
            targets.append(blocks_of[mi][bi])
    system = body.get("system")
    if isinstance(system, list):
        rendered = iter(system)
        cached_system: list[dict[str, Any]] = []
        for part in request.system:
            if not part.text:
                continue
            block = next(rendered)
            if part.cache:
                cached_system.append(block)
        targets.extend(reversed(cached_system))

    stamped: list[dict[str, Any]] = []
    for target in targets:
        if len(stamped) >= _MAX_CACHE_BREAKPOINTS:
            break
        if any(target is seen for seen in stamped):
            continue
        target["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)
        stamped.append(target)


def _image_to_wire(block: Image) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": block.media_type,
            "data": base64.b64encode(block.data).decode("ascii"),
        },
    }


#: The tags of a local command's message (the caveat, the command, its
#: output): Claude Code sends each as a text block of its own.
_COMMAND_TAGS = ("<local-command-caveat>", "<command-name>", "<local-command-stdout>")


def _command_parts(text: str) -> list[str]:
    """A local command's message cut into one part per tag; any other text
    whole."""
    if not text.startswith(_COMMAND_TAGS):
        return [text]
    parts: list[str] = []
    for para in text.split("\n\n"):
        if parts and not para.startswith(_COMMAND_TAGS):
            parts[-1] += "\n\n" + para
        else:
            parts.append(para)
    return parts


def _wrap_system_reminder(text: str) -> str:
    """Anthropic-only tag syntax. The wrapped text is neutralised first:
    several host-authored turns stand in for text produced elsewhere (a
    background job's stdout, a sub-agent's answer, a recalled memory body),
    and this wrapper is the only thing allowed to open or close the turn."""
    return f"<system-reminder>\n{neutralize_reserved_tags(text)}\n</system-reminder>"


def _message_to_wire(message: Message, vision: bool) -> dict[str, Any]:
    if message.role == "assistant":
        return _assistant_to_wire(message)
    if message.role == "system":
        return {"role": "system", "content": [
            {"type": "text", "text": neutralize_reserved_tags(b.text)}
            for b in message.content if isinstance(b, Text) and b.text]}
    return _user_to_wire(message, vision)


def _user_to_wire(message: Message, vision: bool) -> dict[str, Any]:
    """A user message → one Anthropic user turn. ``tool_result`` blocks lead
    the turn (Anthropic wants them first, answering the preceding
    ``tool_use``); text and images follow in order. Thinking / tool-use blocks
    have no meaning in a user turn and are skipped."""
    wrap = is_host_injected(message)
    results: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ToolResult):
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.call_id,
                    "content": _tool_result_content(block, vision),
                    "is_error": block.is_error,
                }
            )
        elif isinstance(block, Text):
            if wrap:
                rest.append({"type": "text", "text": _wrap_system_reminder(block.text)})
            else:
                rest += [{"type": "text", "text": t} for t in _command_parts(block.text)]
        elif isinstance(block, Image):
            rest.append(_image_to_wire(block))
    return {"role": "user", "content": [*results, *rest]}


def _tool_result_content(block: ToolResult, vision: bool) -> Any:
    """``tool_result.content`` accepts a bare string or a block array. With no
    images this is the **string**, keeping the text-only path compact. With
    images and a vision-capable model it is an array: one text block, then one
    base64 image block per image. A catalogued non-vision model gets the string
    plus a note, so it does not act as if the tool returned only text."""
    refs = [b for b in block.content if isinstance(b, ToolReference)]
    if refs:
        return [{"type": "tool_reference", "tool_name": r.tool_name} for r in refs]
    text = tool_result_text(block)
    images = tool_result_images(block)
    if block.block_list and (vision or not images):
        return [{"type": "text", "text": b.text} if isinstance(b, Text) else _image_to_wire(b)
                for b in block.content if isinstance(b, (Text, Image))]
    if not images:
        return text
    if not vision:
        return text + "\n[image omitted: model is not vision-capable]"
    return [{"type": "text", "text": text}, *(_image_to_wire(i) for i in images)]


def _assistant_to_wire(message: Message) -> dict[str, Any]:
    """Regroup content into Anthropic's required order ``thinking* / text* /
    image* / tool_use*``, stable within each group. Thinking another family
    produced never goes back: its signature is not one Anthropic issued and
    Anthropic rejects foreign or unsigned thinking. A block of unknown family
    (recorded without one) still goes."""
    thinking: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, Thinking):
            if block.family not in (None, FAMILY):
                continue
            if block.data is not None:
                # A redacted (encrypted) reasoning block: re-emit the opaque
                # blob verbatim under its own wire type, never as a
                # ``thinking`` block (an empty-text one would be rejected).
                thinking.append({"type": "redacted_thinking", "data": block.data})
                continue
            entry: dict[str, Any] = {"type": "thinking", "thinking": block.text}
            if block.signature is not None:
                entry["signature"] = block.signature
            thinking.append(entry)
        elif isinstance(block, Text):
            # The API rejects an empty text block, so one never goes out.
            if block.text.strip():
                texts.append({"type": "text", "text": block.text})
        elif isinstance(block, Image):
            images.append(_image_to_wire(block))
        elif isinstance(block, ToolUse):
            calls.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return {"role": "assistant", "content": [*thinking, *texts, *images, *calls]}
