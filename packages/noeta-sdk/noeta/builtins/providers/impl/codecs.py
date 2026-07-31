"""Tool-argument JSON codec and ``Retry-After`` parsing, shared by the provider
adapters.

Only steps that are genuinely independent of any wire shape belong here; each
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
    "encode_tool_arguments",
    "decode_tool_arguments",
    "parse_retry_after",
]


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
