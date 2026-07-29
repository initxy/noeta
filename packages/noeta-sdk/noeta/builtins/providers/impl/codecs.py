"""Shared provider-adapter logic: JSON encode/decode of tool-call arguments,
plus ``Retry-After`` header parsing.

Provider neutrality keeps each provider's wire translation sealed
inside its own ``noeta.providers.<name>`` adapter. But a few translation steps are
**genuinely shared** and independent of any wire shape — notably converting
tool-call arguments between a Noeta-shape ``dict`` and a JSON string:

  * Outbound: ``ToolUseBlock.arguments`` (``dict``) → JSON string for the wire
    (OpenAI Chat's ``function.arguments`` and Responses' ``function_call.arguments``
    are both strings).
  * Inbound: wire arguments string → ``dict`` (a missing string normalizes to
    ``{}``; a decode failure raises ``ValueError``, which the outer
    ``RuntimeLLMClient`` turns into ``stop_reason="error"``).

This snippet used to be copied into both the ``openai_compat`` and
``openai_responses`` adapters (``json.dumps`` / ``json.loads`` plus identical
defaulting and error logic). It is collapsed here into a deep module: a tiny
interface (the ``encode`` / ``decode`` functions) hiding the defaulting, error-message
assembly, and exception-type narrowing behind it.

**Provider-neutral red line:** this collects
**only** the genuinely shared mechanical snippet. Each adapter's **real differences**
stay in that adapter — how the default is taken (``or "{}"`` vs ``is None``), the
inbound error prefix (``tool_call arguments`` vs ``function_call arguments``), and
Anthropic's inbound path which uses no JSON string at all (``tool_use.input`` is
already a ``dict`` and never touches this codec). The adapter feeds its own shape
to this codec. So ``decode`` takes the raw value the adapter already extracted plus
an ``error_label`` prefix; this codec decides for no one how to extract it or which
prefix to use.
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
    """``ToolUseBlock.arguments`` (Noeta-shape ``dict``) → wire JSON string.

    Both OpenAI Chat and Responses serialize tool-call arguments to a string on the
    wire. This is a one-line ``json.dumps``, but collapsing it to a single point
    defines the encoding convention in exactly one place (default separators, no
    forced ASCII) — the one spot to watch if these providers ever want a unified
    caching key.
    """
    return json.dumps(arguments)


def decode_tool_arguments(raw: Optional[str], *, error_label: str) -> dict[str, Any]:
    """wire arguments string → Noeta-shape ``dict`` (inbound).

    A missing value (``None``) normalizes to the empty object ``{}``. A
    ``json.loads`` failure (including the ``TypeError`` from feeding in a non-string)
    raises :class:`MalformedToolArgumentsError` worded
    ``"<error_label> not JSON-decodable: <exc>"``. That class subclasses
    :class:`ValueError` (so this wording/type contract is unchanged for any
    ``except ValueError`` caller) **and** :class:`TransientError`: a non-decodable
    arguments string is in practice a truncated/garbled stream, so the outer
    ``RuntimeLLMClient`` retries it on its transient budget instead of failing the
    whole task on one flaky response. The adapter passes ``error_label`` so each
    provider's **original error wording stays byte-for-byte intact** (OpenAI Chat
    uses ``"tool_call arguments"``, Responses uses ``"function_call arguments"``) —
    this codec pins no prefix for anyone; that is an adapter wire-vocabulary
    difference. When the transient budget is
    exhausted the error is turned into ``stop_reason="error"`` by the outer
    ``RuntimeLLMClient``.

    Note that the default applies **only** to ``None``: an empty string ``""`` and
    the like still go to ``json.loads`` (an empty string simply fails to parse →
    error). If a provider wants to treat the empty string as a default too (as OpenAI
    Chat historically did with ``function.get("arguments") or "{}"``), that is an
    adapter extraction difference and should be resolved on the adapter side before
    being passed in; this codec does not swallow empty strings on its own.
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

    Both wire forms are handled: the ``delta-seconds`` form (``"120"``) and the
    ``HTTP-date`` form (``"Wed, 21 Oct 2026 07:28:00 GMT"``). The date form is
    converted to a delay from *now* and clamped to ``>= 0`` (a date already in the
    past means "retry now"). An absent or unparseable header returns ``None`` so the
    runtime's :func:`retry_policy` falls back to exponential backoff.

    Each provider's 429 translation used to copy an integer-only stub of this with
    the HTTP-date branch left as a TODO; collapsing it here gives all three adapters
    full RFC-7231 coverage from one place. Reading the clock is intentional and lives
    on the adapter (LIVE) side — the retry sleeps it feeds write no events
    (README D-2d), so this stays off the deterministic fold path. ``now`` is injected
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
