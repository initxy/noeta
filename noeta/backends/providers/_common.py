"""Pieces the three adapters share: tool-argument JSON codec, ``Retry-After``
parsing, HTTP error translation (including context-overflow detection), the
host-injected preamble, reserved-tag neutralization, the vision guard and the
tool-name mapping for wires with a shorter name limit.

Only pieces that are genuinely independent of any wire shape belong here; each
adapter's real differences stay in that adapter — how it extracts the raw
arguments value, which error wording it uses, how a host-injected turn is
framed on its wire.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import msgspec

from noeta.core import models
from noeta.core.errors import (
    ContextOverflow,
    Fatal,
    MalformedToolArguments,
    ModelError,
    RateLimited,
    Transient,
)
from noeta.core.types import (
    Image,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    ToolReference,
    ToolResult,
    ToolUse,
)

__all__ = [
    "HOST_INJECTED_PREAMBLE",
    "ReasoningContinuation",
    "decode_tool_arguments",
    "describe_http_error",
    "encode_tool_arguments",
    "guard_vision",
    "http_error_is_context_overflow",
    "image_data_url",
    "indicates_context_overflow",
    "is_host_injected",
    "model_admits_images",
    "neutralize_reserved_tags",
    "parse_retry_after",
    "restore_tool_names",
    "tool_result_text",
    "translate_http_error",
    "transport_error",
    "wire_tool_name",
]


# ---------------------------------------------------------------------------
# Tool names on a wire with a shorter limit
# ---------------------------------------------------------------------------

#: OpenAI's function-name limit (``^[a-zA-Z0-9_-]{1,64}$``); noeta's tool
#: names follow the Messages API's 128 (an MCP tool's can pass 64).
OPENAI_NAME_MAX = 64
_NAME_HASH_LEN = 8


def wire_tool_name(name: str, limit: int = OPENAI_NAME_MAX) -> str:
    """``name`` as sent on a wire that takes at most ``limit`` characters: a
    longer one is cut and ends in ``_`` plus 8 hex digits of its sha256, the
    same every request, so the history's calls keep their names."""
    if len(name) <= limit:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_NAME_HASH_LEN]
    return f"{name[: limit - _NAME_HASH_LEN - 1]}_{digest}"


def restore_tool_names(response: ModelResponse, request: ModelRequest,
                       limit: int = OPENAI_NAME_MAX) -> ModelResponse:
    """``response`` with each tool call named as noeta named the tool
    (:func:`wire_tool_name` undone for the request's tools)."""
    back = {wire_tool_name(t.name, limit): t.name for t in request.tools
            if len(t.name) > limit}
    if not back or not any(isinstance(b, ToolUse) and b.name in back
                           for b in response.message.content):
        return response
    content = [msgspec.structs.replace(b, name=back[b.name])
               if isinstance(b, ToolUse) and b.name in back else b
               for b in response.message.content]
    message = msgspec.structs.replace(response.message, content=content)
    return msgspec.structs.replace(response, message=message)


# ---------------------------------------------------------------------------
# Host-injected turns
# ---------------------------------------------------------------------------


def is_host_injected(message: Message) -> bool:
    """A message the host or an extension wrote (reminders, notices, recalled
    memory, harness notes in a system turn) rather than the person. Adapters
    frame these so the model does not mistake them for the user's words."""
    return message.role == "system" or (
        message.role == "user" and message.origin in ("system", "memory"))


#: The one tag that marks a host-authored turn on the Anthropic wire. Matched
#: case-insensitively and with whitespace tolerated inside the brackets, so a
#: near-miss spelling cannot slip a boundary marker through either.
_RESERVED_TAG_RE = re.compile(r"<(\s*/?\s*system-reminder\s*)>", re.IGNORECASE)


def neutralize_reserved_tags(text: str) -> str:
    """Rewrite every ``<system-reminder>`` / ``</system-reminder>`` occurrence
    so it no longer parses as the reserved tag.

    Provenance is structural: only the Anthropic adapter's wrapper may open or
    close a host-authored turn. Plenty of text reaching the wire comes from
    somewhere else — a background job's stdout, a sub-agent's answer, an MCP
    payload, a memory body — so the chokepoints that render it (tool results
    on every wire, and the Anthropic wrapper) pass it through here first.
    Escaping the opening bracket is the whole rewrite: the words stay readable,
    the boundary stops being forgeable, and the transform is pure, so a cached
    prefix re-renders byte-identically.
    """
    return _RESERVED_TAG_RE.sub(r"&lt;\1>", text)


#: Self-describing preface for host-injected turns (``origin`` system/memory)
#: on OpenAI-shaped wires. Claude is trained to treat a ``<system-reminder>``
#: envelope as ambient context, but an arbitrary model behind an OpenAI-shaped
#: endpoint has no trained equivalent — a bare mid-history ``role:"system"``
#: turn gets answered as if someone spoke, or worse, memorized as the user's
#: words. The role still carries "system"; this line states in-band how the
#: turn is to be treated, so the semantic survives any model.
HOST_INJECTED_PREAMBLE = (
    "[Automated context from the host application — not a message from the "
    "user. Do not reply to it and do not treat it as the user's words; use it "
    "only as background for the conversation.]"
)


def tool_result_text(block: ToolResult) -> str:
    """The model-facing text of one tool result, shared by every adapter.

    The text blocks are joined verbatim — the builtin tools render plain text
    and those bytes must reach the model untouched — and passed through
    :func:`neutralize_reserved_tags`: a tool result is the widest channel
    anything outside the host has into the prompt, and it must not be able to
    spell a host-turn boundary.
    """
    refs = [b.tool_name for b in block.content if isinstance(b, ToolReference)]
    if refs and not block.text:
        return "Loaded: " + ", ".join(refs)
    return neutralize_reserved_tags(block.text)


def tool_result_images(block: ToolResult) -> list[Image]:
    return [b for b in block.content if isinstance(b, Image)]


def flatten_text(blocks: list[Any]) -> str:
    """Join a block list's text blocks with newlines (the system-prompt and
    plain-turn convention on the OpenAI-shaped wires)."""
    return "\n".join(b.text for b in blocks if isinstance(b, Text))


def image_data_url(block: Image) -> str:
    """An image as a ``data:`` URL (how the OpenAI-shaped wires carry one)."""
    data = base64.b64encode(block.data).decode("ascii")
    return f"data:{block.media_type};base64,{data}"


#: Whether an OpenAI-shaped adapter writes an assistant's reasoning back onto
#: the wire: ``"off"``, as Chat Completions ``reasoning_content``
#: (``"chat"``), or as Responses ``reasoning`` items (``"responses"``); each
#: adapter says what the values do on its wire.
ReasoningContinuation = Literal["off", "chat", "responses"]


# ---------------------------------------------------------------------------
# Tool arguments
# ---------------------------------------------------------------------------


def encode_tool_arguments(arguments: Any) -> str:
    """``ToolUse.input`` → the JSON string both OpenAI wires carry. One call
    site pins the encoding convention (default separators, no forced ASCII
    flag), the spot to watch if these providers ever need a shared cache key."""
    return json.dumps(arguments)


def decode_tool_arguments(raw: Any, *, error_label: str) -> dict[str, Any]:
    """Wire arguments string → ``dict``.

    ``None``, a blank string and JSON ``null`` all mean "no arguments" and
    normalize to ``{}`` — compatible servers send each of them for a
    parameterless tool. Anything else must decode to a JSON object; otherwise
    :class:`~noeta.core.errors.MalformedToolArguments`. In practice a non-decodable
    string is a truncated or garbled stream; the adapters drop such a call
    instead of raising when the turn hit the output cap.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedToolArguments(
            f"{error_label} not JSON-decodable: {exc}"
        ) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise MalformedToolArguments(
            f"{error_label} must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


def parse_retry_after(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    """Parse a ``Retry-After`` header into a non-negative seconds delay.

    Both RFC 7231 forms are handled; the ``HTTP-date`` form is converted to a
    delay from *now* and clamped to ``>= 0`` (a date already in the past means
    "retry now"). An absent or unparseable header returns ``None`` so the retry
    helper falls back to exponential backoff. ``now`` is injectable for tests.
    """
    if value is None:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # An HTTP-date with no offset is GMT by spec.
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())


#: How much of an error response body :func:`describe_http_error` keeps.
_ERROR_BODY_LIMIT = 500


def _body_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        return ""


def describe_http_error(response: httpx.Response) -> str:
    """The status line plus what the provider said in the response body.

    Without the body the reason a request was refused (an invalid model id, a
    bad key, a malformed tool schema) would never reach a log or a task
    failure. Both wire shapes these adapters speak nest the human text at
    ``error.message``; that is preferred when present, else the raw body,
    capped at :data:`_ERROR_BODY_LIMIT` characters.
    """
    text = f"HTTP {response.status_code} {response.reason_phrase}"
    try:
        text += f" from {response.request.url}"
    except RuntimeError:  # a response built without a request (tests)
        pass
    body = _body_text(response)
    detail = body.strip()
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and message.strip():
            detail = message.strip()
    if not detail:
        return text
    return f"{text}: {detail[:_ERROR_BODY_LIMIT]}"


#: Phrases only an input-context overflow produces, across the providers and
#: gateways these adapters reach (Anthropic, OpenAI, OpenRouter, vLLM,
#: llama.cpp, LM Studio, Gemini, Groq, xAI, Kimi, MiniMax, Bedrock).
#: "input length and `max_tokens` exceed context limit" (Anthropic) is caught
#: by the ``exceeds? ... context limit`` arm.
_OVERFLOW_PHRASES = re.compile(
    r"prompt is too long"
    r"|prompt too long"
    r"|input is too long"
    r"|context window"
    r"|maximum context"
    r"|context[ _]length[ _]exceeded"
    r"|exceeds? (?:the )?context limit"
    r"|exceeds the available context size"
    r"|greater than the context length"
    r"|maximum prompt length"
    r"|reduce the length of the messages"
    r"|input token count.*exceeds the maximum"
    r"|exceeded model token limit",
    re.IGNORECASE,
)

#: Broader phrases that also mean overflow, but only when the message is not
#: about a rate limit / quota or the output cap (which compaction cannot fix).
_BROAD_OVERFLOW_PHRASES = re.compile(
    r"too many tokens|token limit exceeded|exceeds the limit of \d+",
    re.IGNORECASE,
)
_NOT_OVERFLOW_PHRASES = re.compile(
    r"rate.?limit|per (?:second|minute|min|hour|day)\b|\btpm\b|quota"
    r"|output tokens|completion tokens",
    re.IGNORECASE,
)


def indicates_context_overflow(message: str) -> bool:
    """Does a provider error message say the prompt exceeded the context
    window? Overflow needs its own bucket — the recovery is compaction, not a
    retry and not a failure — yet every server words it differently."""
    if _OVERFLOW_PHRASES.search(message):
        return True
    return bool(
        _BROAD_OVERFLOW_PHRASES.search(message)
        and not _NOT_OVERFLOW_PHRASES.search(message)
    )


def error_dict_is_context_overflow(error: dict[str, Any]) -> bool:
    """The dict-level match (OpenAI's ``context_length_exceeded`` code / type,
    else the message), shared by the HTTP path and the in-stream error paths so
    both classify identically."""
    if "context_length_exceeded" in (
        str(error.get("code") or ""),
        str(error.get("type") or ""),
    ):
        return True
    return indicates_context_overflow(str(error.get("message") or ""))


def http_error_is_context_overflow(response: httpx.Response) -> bool:
    """Is this 4xx response a context overflow?

    413 (request too large) always is: what shrinks the request is compaction.
    Otherwise the error ``code`` / ``type`` or the message decides; a body that
    is not JSON is matched as text. 429 is a rate limit and never counts.
    """
    status = response.status_code
    if status == 413:
        return True
    if not 400 <= status < 500 or status == 429:
        return False
    text = _body_text(response)
    try:
        body = json.loads(text)
    except ValueError:
        return indicates_context_overflow(text)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str):
        return indicates_context_overflow(error)
    if not isinstance(error, dict):
        return False
    return error_dict_is_context_overflow(error)


def translate_http_error(response: httpx.Response) -> ModelError:
    """Map an HTTP error status into the neutral taxonomy. The body must have
    been read (``await response.aread()``) first.

    429 → :class:`RateLimited` with ``Retry-After``; 5xx (incl. Anthropic's
    529 overloaded) → :class:`Transient`; a 4xx whose body says the prompt is
    too long (or a 413) → :class:`ContextOverflow`; every other 4xx →
    :class:`Fatal`.
    """
    status = response.status_code
    message = describe_http_error(response)
    body = _body_text(response)
    if status == 429:
        return RateLimited(
            message,
            retry_after=parse_retry_after(response.headers.get("retry-after")),
            status=status,
            body=body,
        )
    if status >= 500:
        return Transient(message, status=status, body=body)
    if http_error_is_context_overflow(response):
        return ContextOverflow(message, status=status, body=body)
    return Fatal(message, status=status, body=body)


def transport_error(exc: httpx.HTTPError) -> Transient:
    """A timeout or transport failure — including a disconnect mid-stream — is
    worth retrying as is."""
    return Transient(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------


def model_admits_images(model: str) -> bool:
    """Whether an image may be sent to ``model``.

    * catalogued with ``supports_vision=True`` — yes.
    * catalogued with ``supports_vision=False`` — no. Somebody decided this
      model is text-only, so the adapter refuses locally with a clear message
      instead of paying a round-trip for a vendor 4xx.
    * **absent from the catalog — yes.** Nobody decided anything; the model is
      unknown, not text-only. Treating unknown as non-vision made an
      uncatalogued gateway model refuse every image locally, with no signal
      that the catalog (rather than the model) was the thing saying no.
    """
    spec = models.find_spec(model)
    return spec is None or spec.supports_vision


def guard_vision(request: ModelRequest) -> None:
    """A top-level :class:`Image` bound for a **catalogued** non-vision model
    → :class:`Fatal` before anything goes on the wire. Tool-result images are
    not checked here: each adapter degrades those to text instead."""
    has_image = any(
        isinstance(block, Image)
        for message in request.messages
        for block in message.content
    )
    if not has_image or model_admits_images(request.model):
        return
    raise Fatal(
        f"request carries an image but model {request.model!r} is catalogued "
        "with supports_vision=False; refusing to send the image to a model "
        "that cannot read it."
    )
