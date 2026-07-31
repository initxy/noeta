"""The canonical Noeta-shape form of LLM data.

Deliberately modelled after no single provider's wire format: each ``Provider``
adapter translates between this shape and its vendor protocol, and Engine /
Policy only ever see the dataclasses defined here. Every typed value that
travels through ContentStore must declare ``__canonical_tag__`` and register a
restorer with :mod:`noeta.protocols.canonical` — miss that and a round-trip
collapses the typed Block into a plain field dict, breaking ``isinstance``
checks upstream. Field names match
:class:`noeta.protocols.decisions.ToolCall` so a ToolUseBlock and its matching
ToolCall share one vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Literal,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)

from noeta.protocols.canonical import register
from noeta.protocols.values import ContentRef


# ---------------------------------------------------------------------------
# Block variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str

    __canonical_tag__ = "text_block"


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """Provider-opaque reasoning continuation block.

    ``text`` is the visible reasoning surface; ``signature`` is the opaque
    continuation token the provider hands back so the next turn can resume
    reasoning, and it must round-trip verbatim — providers reject re-uploaded
    thinking blocks whose signature is missing or mutated. ``data`` carries a
    provider's *redacted* reasoning (an encrypted blob its safety system
    substituted for the visible text): when set, ``text`` is empty and the only
    legal round-trip is re-emitting the blob byte-for-byte.

    Non-reasoning models never produce this block.
    """

    text: str
    signature: Optional[str] = None
    data: Optional[str] = None

    __canonical_tag__ = "thinking_block"
    __canonical_omit_none__ = frozenset({"data"})


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """LLM asks the runtime to invoke a tool.

    ``call_id`` is the same id surface as
    :class:`noeta.protocols.decisions.ToolCall.call_id`; Engine pairs a
    ToolUseBlock to its matching ToolResultBlock on this field alone.
    """

    call_id: str
    tool_name: str
    arguments: dict[str, Any]

    __canonical_tag__ = "tool_use_block"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """Tool runtime returns its result for a given ``call_id``.

    ``output`` is typically a ``str`` / ``dict`` / ``ContentRef``; large bodies
    live in ContentStore behind a ContentRef rather than inline.

    ``images`` carries image content the tool surfaces for a vision model to
    *see* (e.g. the ``read`` tool reading a ``.png``). The bound adapter
    deref→base64-inlines the bytes at wire time into the provider's tool-result
    image slot, and the degrade for a non-vision model lives in that adapter —
    only the wire layer knows which model is bound, so it must not be decided
    here.
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

    The ledger carries only the content-addressed ``ContentRef`` handle; the
    real bytes live in ContentStore and are **never** written to the ledger.
    The deref + base64 happens only while assembling the outbound wire format,
    transiently, and is never written back. Because ``ContentRef.hash`` is
    deterministic, an ``ImageBlock`` rides into ``request_ref`` byte-stable.
    """

    source: ContentRef

    __canonical_tag__ = "image_block"


Block = Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock, ImageBlock]


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


#: Message author source. ``None`` (default) means the role's natural author;
#: ``system`` / ``memory`` mark host-injected content. Rendering it onto the
#: wire is each adapter's job — vendor tag syntax never enters the ledger.
MessageOrigin = Literal["human", "system", "memory"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in the conversation history.

    Sole-writer guard on ``origin``: only the Engine ledger seam
    (``Engine.append_user_message``) may set it; Policy-supplied messages get
    it stripped at the Decision seams.

    A ``role == "system"`` Message belongs on :attr:`LLMRequest.system` and is
    **not** allowed inside :attr:`LLMRequest.messages`; an adapter that finds
    one in the history array should raise rather than pass it through.
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

    ``model`` is supplied per-request — a provider instance pins no model.
    ``system`` is kept orthogonal to the message history so it can sit in a
    stable_prefix cache without re-entering the rolling
    ``RuntimeState.messages``. ``tools`` holds raw JSON-Schema fragments.
    """

    model: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    system: Optional[Message] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output_schema: Optional[dict[str, Any]] = None
    thinking: Optional[str] = None   # "adaptive" | "disabled" | None
    effort: Optional[str] = None     # "low" | "medium" | "high" | "xhigh" | "max" | None

    __canonical_omit_none__ = frozenset({"output_schema", "thinking", "effort", "max_tokens"})


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-neutral token accounting for one LLM round-trip.

    Each adapter maps its vendor's usage wire shape into these fields; vendor
    names are **never** pinned here. A provider reporting only a flat prompt
    total maps it to ``uncached`` and leaves the cache fields 0.

    ``input`` is a derived property, not a stored field, so it can never
    disagree with its parts and never enters the canonical byte stream.
    ``visible_output`` discounts a reasoning model's hidden chain-of-thought so
    it does not count against the user-facing answer size.

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

    Adapters normalise their vendor's terminal signal into the Noeta-shape
    ``stop_reason`` vocabulary. ``raw`` preserves the original provider
    response dict for diagnostics only — it is not part of the persisted
    recording.
    """

    stop_reason: Literal["tool_use", "end_turn", "max_tokens", "error"]
    content: list[Block]
    usage: Usage = field(default_factory=Usage)
    raw: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Streaming deltas
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """One incremental fragment of an in-flight LLM response.

    Deltas are **ephemeral**: they never enter EventLog / ContentStore, hence
    no ``__canonical_tag__`` and no canonical registration. The durable record
    of the same bytes is the final :class:`LLMResponse`, so a consumer must
    tolerate losing deltas entirely.

    ``index`` is the content-block index within the response, letting a
    consumer keep interleaved text/thinking blocks apart. Tool-call arguments
    are never surfaced as deltas — partial argument JSON is undecodable, so
    adapters accumulate it silently until the block completes.
    """

    kind: Literal["text", "thinking"]
    text: str
    index: int


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """Third-party LLM provider shape.

    Providers are pure: they do **not** write the EventLog, do **not** consume
    a :class:`noeta.protocols.step_context.StepContext`, and know nothing about
    resume. Recording lives in the runtime-side wrapper
    (:class:`noeta.runtime.llm.RuntimeLLMClient`).
    """

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@runtime_checkable
class HeaderAwareProvider(Protocol):
    """Optional capability: a provider that accepts per-request HTTP headers.

    The base :class:`LLMProvider` contract stays a single pure
    ``complete(request)``. A provider talking to a gateway that keys
    tracing/logging off request-scoped headers implements this extra method so
    the runtime can attach those headers *per call* — the shared client is a
    server-level singleton constructed before any ``task_id`` exists, so it
    cannot be rebuilt per task.

    The runtime probes with ``isinstance`` and falls back to plain
    ``complete`` when absent. The header *shape* is never modelled here; the
    host supplies an opaque ``dict[str, str]``, keeping the seam
    provider-neutral.
    """

    def complete_with_headers(
        self,
        request: LLMRequest,
        request_headers: Optional[dict[str, str]],
    ) -> LLMResponse: ...


@runtime_checkable
class StreamingProvider(Protocol):
    """Optional capability: a provider that can stream response deltas.

    Push-shaped on purpose: the call keeps the blocking one-shot contract of
    :meth:`LLMProvider.complete`, still returning the **complete**
    :class:`LLMResponse` with ``on_delta`` firing as a side effect. That is
    what keeps the Engine / Policy / recording / retry paths byte-identical
    whether or not anyone streams; an iterator-shaped API would invert control
    through every layer above.

    ``request_headers`` is folded into this signature rather than composed with
    :class:`HeaderAwareProvider` so the two optional capabilities never form a
    probe matrix; a provider that ignores headers accepts and drops them.

    Adapters must guarantee that the returned ``LLMResponse`` is shape-identical
    to what the batch path would produce for the same content, and that a
    mid-stream transport failure raises through the same neutral error taxonomy
    as ``complete`` so the runtime retry loop applies unchanged.
    """

    def complete_streaming(
        self,
        request: LLMRequest,
        on_delta: Callable[[StreamDelta], None],
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Canonical-tag registration
# ---------------------------------------------------------------------------


register("text_block", lambda f: TextBlock(**f))
register("thinking_block", lambda f: ThinkingBlock(**f))
register("tool_use_block", lambda f: ToolUseBlock(**f))
register("tool_result_block", lambda f: ToolResultBlock(**f))
register("image_block", lambda f: ImageBlock(**f))
register("message", lambda f: Message(**f))
