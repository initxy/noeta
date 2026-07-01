"""Regression tests for provider-adapter fixes (code-review group 'providers').

Three fixes covered:

* openai_responses ``_message_to_responses`` now respects ``message.origin``:
  host-injected turns (origin system/memory) render as ``role:"system"`` input
  items, equivalent to openai_compat raising a system role and anthropic
  wrapping ``<system-reminder>`` (D4);
  a human's own words stay ``role:"user"``.
* catalog ``price`` / ``spec_for`` now ``resolve_alias`` first, so a friendly
  alias (e.g. ``"opus"``) no longer raises KeyError.
* anthropic context-overflow marker set tightened: drop the over-broad
  ``"max tokens"`` / ``"too many tokens"``; a real overflow
  (``"prompt is too long"``) still matches.
"""

from __future__ import annotations

import httpx

from noeta.protocols.messages import Message, TextBlock, Usage
from noeta.providers.anthropic import _is_context_overflow
from noeta.providers.catalog import price, spec_for
from noeta.providers.openai_responses import _message_to_responses


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
                "content": [{"type": "input_text", "text": "be brief"}],
            }
        ]


def test_genuine_user_turn_stays_role_user() -> None:
    for origin in ("human", None):
        items = _message_to_responses(_user("hi there", origin), "off", None)
        assert items[0]["role"] == "user"


# --- catalog: alias pricing -------------------------------------------------


def test_price_resolves_alias() -> None:
    usage = Usage(uncached=1_000_000, cache_read=0, cache_write=0, output=0)
    # alias and resolved real id price identically, byte for byte, no KeyError.
    assert price("opus", usage) == price("claude-opus-4-8", usage)


def test_spec_for_resolves_alias() -> None:
    assert spec_for("opus").real_model_id == "claude-opus-4-8"
    assert spec_for("sonnet") is spec_for("claude-sonnet-4-6")


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
    # Over-broad "max tokens" / "too many tokens" no longer misclassify a plain 400 as overflow.
    assert not _is_context_overflow(_resp_400("exceeds the max tokens allowed"))
    assert not _is_context_overflow(_resp_400("too many tokens in request"))
