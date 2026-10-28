"""Messages, model requests and responses, and the tool-facing value types.

Everything here is a frozen ``msgspec.Struct`` so it round-trips through the
store as JSON without a hand-written codec. Tagged unions carry their tag in a
``type`` field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Union

import msgspec

if TYPE_CHECKING:
    # mypy's dataclass_transform would make every subclass repeat
    # ``frozen=True``; freezing is enforced at runtime below.
    class Struct(msgspec.Struct, kw_only=True, omit_defaults=True):
        pass
else:

    class Struct(msgspec.Struct, frozen=True, kw_only=True, omit_defaults=True):
        """Base for every recorded value: frozen, keyword-only, defaults omitted
        on the wire so adding an optional field never changes old records'
        bytes."""


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
    signature: str | None = None
    data: str | None = None
    family: str | None = None


class Image(Struct, tag="image", tag_field="type"):
    media_type: str
    data: bytes


class ToolUse(Struct, tag="tool_use", tag_field="type"):
    id: str
    name: str
    input: dict[str, Any]


class ToolReference(Struct, tag="tool_reference", tag_field="type"):
    """A tool result naming a deferred tool whose schema is now loaded
    (``noeta.core.deferred``): from the next request on, the tool is sent."""

    tool_name: str


class ToolResult(Struct, tag="tool_result", tag_field="type"):
    call_id: str
    content: list[Text | Image | ToolReference]
    is_error: bool = False
    #: Sent as a list of content blocks even when it is plain text, where an
    #: adapter otherwise sends a string (Claude Code's shape for MCP and
    #: Agent results).
    block_list: bool = False

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, Text))


Block = Union[Text, Thinking, Image, ToolUse, ToolResult]

#: Who wrote a user-role message. ``human`` is the person; ``system`` is the
#: host or an extension speaking in the user channel (reminders, notices);
#: ``memory`` is recalled material; ``agent`` is another agent's message (a
#: sub-agent's hand-back), plain text that frames itself and carries no user
#: authority; ``inline`` is text the host adds to the person's turn as plain
#: text, not as a reminder (a typed command's skill body, the interrupted
#: marker), joined into that turn. Adapters decide how each reaches the wire.
Origin = Literal["human", "system", "memory", "agent", "inline"]


class Message(Struct):
    """A turn of the conversation. ``system`` is a harness note between the
    turns (Claude Code's mid-conversation system message: the environment,
    the token budget, per-step notes); adapters join adjacent ones."""

    role: Literal["user", "assistant", "system"]
    content: list[Block]
    origin: Origin | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, Text))

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [b for b in self.content if isinstance(b, ToolUse)]

    @property
    def from_person(self) -> bool:
        """A user turn the person wrote (``origin`` unset or ``human``), not
        a notice, recalled memory or another agent. Tool results also ride
        in user turns; callers that must exclude them check the content."""
        return self.role == "user" and self.origin in (None, "human")


def user(text: str, origin: Origin | None = None) -> Message:
    return Message(role="user", content=[Text(text=text)], origin=origin)


def system_message(text: str) -> Message:
    return Message(role="system", content=[Text(text=text)], origin="system")


# --------------------------------------------------------------------------
# Model request / response
# --------------------------------------------------------------------------


class ToolSpec(Struct):
    name: str
    description: str
    input_schema: dict[str, Any]
    #: A deferred tool (sent once loaded, see ``noeta.core.deferred``); the
    #: adapter marks it so the vendor keeps it out of the cached prefix.
    defer_loading: bool = False


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
    max_tokens: int | None = None
    effort: Effort | None = None
    #: ``True`` asks for the model's reasoning where it has some, ``False``
    #: switches it off, ``None`` sends nothing (the vendor's default).
    thinking: bool | None = True
    output_schema: dict[str, Any] | None = None
    #: Text that ends the reply where the model writes it (not included).
    stop_sequences: list[str] = msgspec.field(default_factory=list)
    #: Indices into ``messages`` after which the adapter places a cache
    #: breakpoint (the composer decides; adapters that cannot cache ignore it).
    cache_after: list[int] = msgspec.field(default_factory=list)
    #: ``(message index, block index)`` pairs: a breakpoint after that one
    #: content block of a message that has several.
    cache_blocks: list[tuple[int, int]] = msgspec.field(default_factory=list)
    headers: dict[str, str] = msgspec.field(default_factory=dict)
    #: ``"auto"`` / ``"none"`` / ``"any"``; ``None`` sends nothing (the vendor
    #: default). The summarize call sets ``"none"`` so a summarizer cannot
    #: answer with a tool call.
    tool_choice: str | None = None
    #: The task tree the call serves (its root task's id); an adapter may
    #: send it as the vendor's per-conversation id.
    root_id: str | None = None
    #: Vendor opt-ins an adapter adds to its own (Anthropic ``anthropic-beta``
    #: flags); adapters of other vendors ignore them.
    betas: list[str] = msgspec.field(default_factory=list)
    #: Top-level body fields for the vendor (Anthropic ``safeguards``);
    #: adapters of other vendors ignore them.
    vendor_fields: dict[str, Any] = msgspec.field(default_factory=dict)


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
    #: Set by the runner: the log seq of the ``ModelCalled`` this fragment
    #: belongs to, and which attempt of that call (a retry starts over, so a
    #: front end drops the fragments of an earlier attempt).
    call_seq: int = 0
    attempt: int = 1


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
