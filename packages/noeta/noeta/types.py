"""Messages, model requests and responses, and the tool-facing value types.

Everything here is a frozen ``msgspec.Struct`` so it round-trips through the
store as JSON without a hand-written codec. Tagged unions carry their tag in a
``type`` field.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

import msgspec


class Struct(msgspec.Struct, frozen=True, kw_only=True, omit_defaults=True):
    """Base for every recorded value: frozen, keyword-only, defaults omitted on
    the wire so adding an optional field never changes old records' bytes."""


# --------------------------------------------------------------------------
# Content blocks
# --------------------------------------------------------------------------


class Text(Struct, tag="text", tag_field="type"):
    text: str


class Thinking(Struct, tag="thinking", tag_field="type"):
    """Reasoning the provider hands back. ``signature`` / ``data`` are opaque
    continuation tokens that must round-trip byte for byte, and only to the
    provider ``family`` that issued them."""

    text: str = ""
    signature: Optional[str] = None
    data: Optional[str] = None
    family: Optional[str] = None


class Image(Struct, tag="image", tag_field="type"):
    media_type: str
    data: bytes


class ToolUse(Struct, tag="tool_use", tag_field="type"):
    id: str
    name: str
    input: dict[str, Any]


class ToolResult(Struct, tag="tool_result", tag_field="type"):
    call_id: str
    content: list[Union[Text, Image]]
    is_error: bool = False

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, Text))


Block = Union[Text, Thinking, Image, ToolUse, ToolResult]

#: Who wrote a user-role message. ``human`` is the person; ``system`` is the
#: host or an extension speaking in the user channel (reminders, notices);
#: ``memory`` is recalled material. Adapters decide how each reaches the wire.
Origin = Literal["human", "system", "memory"]


class Message(Struct):
    role: Literal["user", "assistant"]
    content: list[Block]
    origin: Optional[Origin] = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, Text))

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [b for b in self.content if isinstance(b, ToolUse)]


def user(text: str, origin: Optional[Origin] = None) -> Message:
    return Message(role="user", content=[Text(text=text)], origin=origin)


# --------------------------------------------------------------------------
# Model request / response
# --------------------------------------------------------------------------


class ToolSpec(Struct):
    name: str
    description: str
    input_schema: dict[str, Any]


class SystemPart(Struct):
    """One block of the system prompt. ``cache`` marks the end of a stable
    prefix the adapter may put a cache breakpoint after."""

    text: str
    cache: bool = False


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelRequest(Struct):
    model: str
    system: list[SystemPart] = msgspec.field(default_factory=list)
    messages: list[Message] = msgspec.field(default_factory=list)
    tools: list[ToolSpec] = msgspec.field(default_factory=list)
    max_tokens: Optional[int] = None
    effort: Optional[Effort] = None
    thinking: bool = True
    output_schema: Optional[dict[str, Any]] = None
    #: Indices into ``messages`` after which the adapter places a cache
    #: breakpoint (the composer decides; adapters that cannot cache ignore it).
    cache_after: list[int] = msgspec.field(default_factory=list)
    headers: dict[str, str] = msgspec.field(default_factory=dict)


class Usage(Struct):
    uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def input(self) -> int:
        return self.uncached + self.cache_read + self.cache_write

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            uncached=self.uncached + other.uncached,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
        )


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


class ModelResponse(Struct):
    message: Message
    stop_reason: StopReason
    usage: Usage = msgspec.field(default_factory=Usage)
    model: str = ""


class Delta(Struct):
    """A streamed fragment. Never stored; the recorded reply is the truth."""

    kind: Literal["text", "thinking"]
    text: str
    index: int = 0


# --------------------------------------------------------------------------
# UI-neutral display data (tool results, extension status items)
# --------------------------------------------------------------------------


class Display(Struct):
    """How a front end may show something, as data. ``kind`` is one of
    ``markdown``, ``code``, ``diff``, ``table``, ``progress``, ``kv``; a front
    end that does not know a kind shows ``fallback``."""

    kind: str
    data: dict[str, Any] = msgspec.field(default_factory=dict)
    fallback: str = ""


def dumps(value: Any) -> bytes:
    return msgspec.json.encode(value)


def loads(data: bytes | str, type_: Any) -> Any:
    return msgspec.json.decode(data, type=type_)
