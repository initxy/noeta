"""Noeta-shape typed LLM protocol (Phase 1 L0).

Defines the *internal canonical form* of LLM-shaped data inside Noeta —
deliberately not modelled after any one provider's wire format. Each
``Provider`` adapter (``OpenAICompatProvider``, future ``AnthropicProvider``)
translates between this shape and its vendor protocol; Engine / Policy
only ever see the dataclasses defined here.

Per SSOT, every typed value that travels through ContentStore
must declare ``__canonical_tag__`` and register a
restorer with :mod:`noeta.protocols.canonical`. Forget to register and
ContentStore round-trips collapse the typed Block back into a plain
field dict, breaking ``isinstance`` checks in Engine / Policy.

Field naming aligns with :class:`noeta.protocols.decisions.ToolCall`
(``call_id`` / ``tool_name`` / ``arguments``) so a ToolUseBlock and its
matching ToolCall share the same vocabulary; provider neutrality pins this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, Union, runtime_checkable

from noeta.protocols.canonical import register
from noeta.protocols.values import ContentRef


# ---------------------------------------------------------------------------
# Block variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A plain-text content block (assistant prose or user input)."""

    text: str

    __canonical_tag__ = "text_block"


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """Provider-opaque reasoning continuation block.

    Covers Anthropic's extended-thinking and OpenAI's reasoning-model
    chain-of-thought signature: ``text`` is the visible reasoning
    surface, ``signature`` is the opaque continuation token the
    provider hands back so the next turn can resume reasoning. The
    signature must round-trip verbatim — providers reject re-uploaded
    thinking blocks whose signature is missing or mutated.

    Non-reasoning models never produce this block.

    ``data`` carries a provider's *redacted* reasoning: an opaque, encrypted
    blob the safety system substituted for the visible reasoning (Anthropic's
    ``redacted_thinking`` block). When set, ``text`` is empty and the block must
    round-trip by re-emitting the blob verbatim (there is nothing human-readable
    to show). Omitted from the canonical form when ``None`` so pre-``data``
    recordings resume byte-identical.
    """

    text: str
    signature: Optional[str] = None
    data: Optional[str] = None

    # Only ``data`` is omit-none: ``signature`` predates the mechanism and was
    # always serialized (as ``null`` when absent), so omitting it now would
    # change existing recordings' canonical bytes. ``data`` is new, so omitting
    # it when ``None`` keeps every pre-``data`` recording byte-identical.
    __canonical_tag__ = "thinking_block"
    __canonical_omit_none__ = frozenset({"data"})


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """LLM asks the runtime to invoke a tool.

    ``call_id`` is the same id surface as
    :class:`noeta.protocols.decisions.ToolCall.call_id` — Engine pairs a
    ToolUseBlock to its matching ToolResultBlock on this field.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]

    __canonical_tag__ = "tool_use_block"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """Tool runtime returns its result for a given ``call_id``.

    ``output`` is typically a ``str`` / ``dict`` / ``ContentRef`` —
    large bodies live in ContentStore via ContentRef.

    ``images`` carries image content the tool surfaces for a vision model to
    *see* (e.g. the ``read`` tool reading a ``.png``). Each ``ImageBlock`` holds
    a small ``ContentRef`` handle; the bound adapter deref→base64-inlines the
    bytes at wire time and places them in the provider's tool-result image slot
    (Anthropic ``tool_result.content`` array / Responses ``function_call_output``
    array). The text tool case leaves it ``None`` — omitted from the canonical
    form (``__canonical_omit_none__``) so pre-image recordings resume
    byte-identical. The non-vision-model degrade lives in each adapter (the
    wire layer knows the bound model), NOT here.
    """

    call_id: str
    output: Any
    success: bool
    error: Optional[str] = None
    images: Optional[list[ImageBlock]] = None

    __canonical_tag__ = "tool_result_block"
    __canonical_omit_none__ = frozenset({"images"})


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """Image input block.

    The ledger carries only a small ``ContentRef`` handle (content-addressed,
    ~100 bytes); the real bytes live in ContentStore and are **never** written
    to the ledger. Media type is already on ``ContentRef.media_type``, not
    duplicated here. The deref + base64 happens only when assembling the
    outbound wire format — a transient step that is not written back to the
    ledger / ContentStore.

    ``ContentRef.hash`` is deterministic (identical bytes yield an identical
    hash), so an ``ImageBlock`` rides into ``request_ref`` byte-stable.
    """

    source: ContentRef

    __canonical_tag__ = "image_block"


Block = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock]


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


#: Message author source. ``None`` (default) =
#: the role's natural author (user = human, assistant = model, tool = tool).
#: ``system`` / ``memory`` mark host-injected system-side content / memory
#: recall. Wire-format rendering is left to each provider adapter (vendor tag
#: syntax never enters the ledger).
MessageOrigin = Literal["human", "system", "memory"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in the conversation history.

    ``role`` is the canonical four-way split aligned with Anthropic +
    OpenAI vocabulary. ``content`` is an ordered list of typed Blocks.

    ``origin`` tags the *author source* of the turn —
    ``None`` (default) means the role's natural author; ``system`` /
    ``memory`` mark host-injected content riding the user channel.
    Sole-writer guard: only the Engine ledger seam
    (``Engine.append_user_message``) may set it; Policy-supplied
    messages get it stripped at the Decision seams. Default is omitted
    from canonical serialization (``__canonical_omit_none__``) so
    pre-origin recordings resume byte-identical.

    Note: ``role == "system"`` Messages live on :attr:`LLMRequest.system`
    and are **not** allowed inside :attr:`LLMRequest.messages`. The
    OpenAI-compat adapter is expected to defensively raise if it sees
    one in the history array.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[Block]
    origin: Optional[MessageOrigin] = None

    __canonical_tag__ = "message"
    __canonical_omit_none__ = frozenset({"origin"})


# ---------------------------------------------------------------------------
# LLM request / response
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Canonical Noeta-shape LLM request.

    ``model`` is supplied per-request (provider instances do not pin a
    model); ``system`` is orthogonal to the message history so it can
    sit in stable_prefix caches without re-entering the rolling
    ``RuntimeState.messages``. ``tools`` is left as ``list[dict]``
    holding JSON-Schema fragments — typed schema modelling lands in a
    later issue.
    """

    model: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    system: Optional[Message] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Optional structured-output / reasoning controls. Omitted from the
    # canonical byte stream when ``None`` so recordings made before these
    # fields existed still resume byte-for-byte identical (omit_none
    # mechanism, see ``canonical.py`` ``OMIT_NONE_KEY``).
    output_schema: Optional[dict[str, Any]] = None
    thinking: Optional[str] = None   # "adaptive" | "disabled" | None
    effort: Optional[str] = None     # "low" | "medium" | "high" | "xhigh" | "max" | None

    __canonical_omit_none__ = frozenset({"output_schema", "thinking", "effort", "max_tokens"})


@dataclass(frozen=True, slots=True)
class Usage:
    """Noeta-shape token accounting for one LLM round-trip.

    Provider-neutral: each adapter maps its vendor's usage
    wire shape into these fields. Anthropic reports cache detail
    (``input_tokens`` is the *uncached* portion plus
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``);
    OpenAI reports a flat total (``prompt_tokens`` → ``uncached``,
    cache fields 0). The vendor names (``total_tokens``,
    ``cache_creation_input_tokens``, …) are **never** pinned as field
    names here.

    ``input`` is a **derived property** (``uncached + cache_read +
    cache_write``), not a stored field — so it can never disagree with
    its parts (no ``__post_init__`` invariant to enforce, no illegal
    instance) and it never enters the canonical byte stream. ``cost``
    accounting ① reads it freely. ``visible_output`` is the user-facing
    completion size, ``max(0, output - reasoning_tokens)``, so a
    reasoning model's hidden chain-of-thought does not count against the
    visible answer.

    Frozen so a recorded ``Usage`` cannot be mutated after the fact.
    """

    uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning_tokens: int = 0

    @property
    def input(self) -> int:
        return self.uncached + self.cache_read + self.cache_write

    @property
    def visible_output(self) -> int:
        return max(0, self.output - self.reasoning_tokens)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Canonical Noeta-shape LLM response.

    ``stop_reason`` is normalised to the Noeta-shape vocabulary
    (``tool_use / end_turn / max_tokens / error``); adapters translate
    from their vendor's terminal signal. ``raw`` preserves the original
    provider response dict for diagnostics only — it is not part of the
    persisted recording.

    ``usage`` is a typed :class:`Usage` (provider-neutral token
    accounting).
    """

    stop_reason: Literal["tool_use", "end_turn", "max_tokens", "error"]
    content: list[Block]
    usage: Usage = field(default_factory=Usage)
    raw: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """Third-party LLM provider shape.

    Providers are pure: they do **not** write the EventLog, do **not**
    consume a :class:`noeta.protocols.step_context.StepContext`, and do
    **not** know about Noeta's Resume mode. Recording lives in
    the runtime-side wrapper (``RuntimeLLMClient`` — issue 12).
    """

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class HeaderAwareProvider(Protocol):
    """Optional capability: a provider that accepts per-request HTTP headers.

    **Most providers don't have this** — the base :class:`LLMProvider`
    contract stays a single pure ``complete(request)``. A provider that
    talks to a gateway keying tracing/logging off request-scoped headers
    (e.g. a session/log id that changes every task) implements this extra
    method so the runtime can attach those headers *per call* without
    rebuilding the shared client (the client is a server-level singleton
    constructed before any ``task_id`` exists).

    The runtime probes for the capability with ``isinstance`` and falls
    back to plain ``complete`` when absent — see
    :func:`noeta.runtime.llm._call_provider`. The header *shape* itself is
    never modelled here; the product host supplies it as an opaque
    ``dict[str, str]``, keeping this seam provider-neutral.
    """

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Canonical-tag registration (SSOT)
# ---------------------------------------------------------------------------


register("text_block", lambda f: TextBlock(**f))
register("thinking_block", lambda f: ThinkingBlock(**f))
register("tool_use_block", lambda f: ToolUseBlock(**f))
register("tool_result_block", lambda f: ToolResultBlock(**f))
register("image_block", lambda f: ImageBlock(**f))
register("message", lambda f: Message(**f))
