"""Three provider-adapter rendering and classification invariants.

* openai_responses ``_message_to_responses`` renders by ``message.origin``:
  host-injected turns (origin system/memory) become ``role:"system"`` input
  items led by ``HOST_INJECTED_PREAMBLE`` — the counterpart of openai_compat
  prefixing a system role and anthropic wrapping ``<system-reminder>`` — while
  a human's own words stay ``role:"user"`` with no preamble.
* catalog ``price`` / ``spec_for`` ``resolve_alias`` first, so a friendly alias
  (e.g. ``"opus"``) prices identically to its real id instead of raising
  KeyError.
* the anthropic context-overflow marker set must be tight: the over-broad
  ``"max tokens"`` / ``"too many tokens"`` must NOT classify as overflow, while
  a real overflow (``"prompt is too long"``) still does.
"""

from __future__ import annotations

import httpx
import pytest

from noeta.protocols.messages import Message, TextBlock, Usage
from noeta.builtins.providers.impl.codecs import (
    http_error_is_context_overflow as _is_context_overflow,
)
from noeta.builtins.providers.impl.catalog import price, spec_for
from noeta.builtins.providers.impl.codecs import HOST_INJECTED_PREAMBLE
from noeta.builtins.providers.impl.openai_responses import _message_to_responses


# --- openai_responses: origin rendering -------------------------------------


def _user(text: str, origin: str | None) -> Message:
    return Message(role="user", content=[TextBlock(text=text)], origin=origin)


def test_host_injected_user_turn_renders_as_system_input_item() -> None:
    for origin in ("system", "memory"):
        items = _message_to_responses(_user("be brief", origin), "off", None)
        assert items == [
            {
                "type": "message",
                "role": "system",
                "content": [
                    {"type": "input_text", "text": HOST_INJECTED_PREAMBLE},
                    {"type": "input_text", "text": "be brief"},
                ],
            }
        ]


def test_genuine_user_turn_stays_role_user() -> None:
    for origin in ("human", None):
        items = _message_to_responses(_user("hi there", origin), "off", None)
        assert items[0]["role"] == "user"
        # A human's own words carry no preamble — it marks host injections only.
        assert items[0]["content"] == [{"type": "input_text", "text": "hi there"}]


# --- catalog: alias pricing -------------------------------------------------


def test_price_resolves_alias() -> None:
    from noeta.builtins.providers.impl.catalog import ALIASES

    usage = Usage(uncached=1_000_000, cache_read=0, cache_write=0, output=0)
    # alias and resolved real id price identically, byte for byte. Written
    # against the alias TABLE, not a pinned id: two generations can share a
    # price, which would make a hard-coded id pass for the wrong reason.
    assert price("opus", usage) == price(ALIASES["opus"], usage)
    assert price("opus", usage) > 0.0


def test_spec_for_resolves_alias() -> None:
    # The aliases track the current generation (see ``ALIASES``); what this
    # case pins is that ``spec_for`` goes THROUGH the table rather than around
    # it, so it is written against the table instead of a frozen id.
    from noeta.builtins.providers.impl.catalog import ALIASES

    assert spec_for("opus").real_model_id == ALIASES["opus"]
    assert spec_for("sonnet") is spec_for(ALIASES["sonnet"])


# --- anthropic: overflow marker tightening ----------------------------------


def _resp_400(message: str) -> httpx.Response:
    return httpx.Response(
        status_code=400,
        json={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )


def test_real_overflow_still_classified() -> None:
    assert _is_context_overflow(
        _resp_400("prompt is too long: 250000 tokens > 200000 maximum")
    )


def test_over_broad_token_phrases_no_longer_misclassified() -> None:
    # "max tokens" alone, the output cap and rate limits are not overflow:
    # compaction cannot fix any of them.
    assert not _is_context_overflow(_resp_400("exceeds the max tokens allowed"))
    assert not _is_context_overflow(
        _resp_400("too many tokens: max_tokens 200000 > 64000 output tokens")
    )
    assert not _is_context_overflow(
        _resp_400("Too many tokens per minute: rate limit reached")
    )


@pytest.mark.parametrize(
    "message",
    [
        # Anthropic, when the input fits but input + max_tokens does not.
        "input length and `max_tokens` exceed context limit: 188240 + 21333 "
        "> 200000, decrease input length or `max_tokens` and try again",
        "This model's maximum context length is 8192 tokens. However, you "
        "requested 9000 tokens",
        "context_length_exceeded",
        "the request exceeds the available context size, try increasing it",
        "Input is too long for requested model.",
        "too many tokens in request",
        "Please reduce the length of the messages or completion.",
        "Your input exceeds the context window of this model",
    ],
)
def test_overflow_wordings_across_providers(message: str) -> None:
    assert _is_context_overflow(_resp_400(message))


def test_413_and_plain_text_bodies_count_as_overflow() -> None:
    assert _is_context_overflow(httpx.Response(status_code=413, text="Request Entity Too Large"))
    assert _is_context_overflow(
        httpx.Response(status_code=400, text="prompt is too long for this model")
    )
    assert not _is_context_overflow(
        httpx.Response(status_code=429, json={"error": {"message": "prompt is too long"}})
    )
