"""Tool-argument JSON codec, ``Retry-After`` parsing, and the host-injected
preamble, shared by the provider adapters.

Only pieces that are genuinely independent of any wire shape belong here; each
adapter's real differences stay in that adapter — how it extracts the raw
arguments value, which error wording it uses, and Anthropic's inbound path,
which carries ``tool_use.input`` as a ``dict`` and never touches this codec.
That is why ``decode_tool_arguments`` takes the already-extracted value plus an
``error_label``: it decides for no one how to extract or how to word.
"""

from __future__ import annotations

import email.utils
import json
from datetime import datetime, timezone
from typing import Any, Optional

from noeta.protocols.errors import MalformedToolArgumentsError


__all__ = [
    "HOST_INJECTED_PREAMBLE",
    "encode_tool_arguments",
    "decode_tool_arguments",
    "parse_retry_after",
    "render_tool_result_body",
]


#: Self-describing preface for host-injected turns (``origin`` system/memory)
#: on OpenAI-shaped wires. Claude is trained to treat a ``<system-reminder>``
#: envelope as ambient context, but an arbitrary model behind an OpenAI-shaped
#: endpoint has no trained equivalent — a bare mid-history ``role:"system"``
#: turn gets answered as if someone spoke, or worse, memorized as the user's
#: words. The role still carries "system"; this line states in-band how the
#: turn is to be treated, so the semantic survives any model. The wording is
#: wire-shape-independent (Chat prefixes it, Responses prepends a segment),
#: which is why it lives here and not in either adapter.
HOST_INJECTED_PREAMBLE = (
    "[Automated context from the host application — not a message from the "
    "user. Do not reply to it and do not treat it as the user's words; use it "
    "only as background for the conversation.]"
)


def render_tool_result_body(output: Any, error: Optional[str]) -> str:
    """The model-facing text of one tool result, shared by every adapter.

    A ``str`` output passes through verbatim — the builtin tools render plain
    text and those bytes must reach the model untouched. A non-string output
    (a host or MCP tool returning a structured value) is JSON-encoded with
    ``ensure_ascii=False`` so non-ASCII content costs its UTF-8 size instead of
    a ~6x ``\\uXXXX`` expansion. A failed call's ``error`` text leads the body:
    OpenAI-shaped wires have no error flag, so the text itself is the only
    channel that survives every provider.
    """
    if isinstance(output, str):
        body = output
    else:
        body = json.dumps(output, ensure_ascii=False)
    if error:
        return f"{error}\n{body}" if body else error
    return body


def encode_tool_arguments(arguments: Any) -> str:
    """``ToolUseBlock.arguments`` → the JSON string both OpenAI Chat and
    Responses put on the wire.

    One call site pins the encoding convention (default separators, no forced
    ASCII) — the spot to watch if these providers ever need a shared cache key.
    """
    return json.dumps(arguments)


def decode_tool_arguments(raw: Optional[str], *, error_label: str) -> dict[str, Any]:
    """Wire arguments string → Noeta-shape ``dict``; ``None`` normalizes to ``{}``.

    A decode failure raises :class:`MalformedToolArgumentsError`, which
    subclasses both :class:`ValueError` and :class:`TransientError`: a
    non-decodable arguments string is in practice a truncated or garbled
    stream, so ``RuntimeLLMClient`` retries it on its transient budget instead
    of failing the whole task on one flaky response (once that budget is spent
    it becomes ``stop_reason="error"``).

    The default applies to ``None`` only — an empty string still reaches
    ``json.loads`` and fails there — so an adapter that wants ``""`` treated as
    absent must normalize before calling. ``error_label`` prefixes the message
    so each adapter keeps its own wire vocabulary.
    """
    if raw is None:
        raw = "{}"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise MalformedToolArgumentsError(
            f"{error_label} not JSON-decodable: {exc}"
        ) from exc


def parse_retry_after(
    value: Optional[str], *, now: Optional[datetime] = None
) -> Optional[float]:
    """Parse a ``Retry-After`` header into a non-negative seconds delay (RFC 7231).

    Both wire forms are handled; the ``HTTP-date`` form is converted to a delay
    from *now* and clamped to ``>= 0`` (a date already in the past means "retry
    now"). An absent or unparseable header returns ``None`` so the runtime's
    :func:`retry_policy` falls back to exponential backoff.

    Reading the clock is safe here: the retry sleeps this feeds write no
    events, so it stays off the deterministic fold path. ``now`` is injected
    only so tests can pin a fixed instant.
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
        # An HTTP-date with no offset is GMT by spec; pin UTC so the subtraction
        # below is tz-aware on both sides.
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (when - current).total_seconds())
