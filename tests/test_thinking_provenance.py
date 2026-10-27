"""A thinking block goes back only to the provider that produced it.

A signature or redacted blob is meaningful only to the provider that issued
it, and Anthropic rejects foreign or unsigned thinking — so after a mid-task
model switch (DeepSeek ``reasoning_content`` → Claude, Responses ciphertext →
Claude) each adapter drops the other providers' blocks on the way out. A block
recorded before ``ThinkingBlock.provider`` existed decodes with
``provider=None`` and is still sent as before.
"""

from __future__ import annotations

from noeta.builtins.providers.impl.anthropic import _assistant_message_to_anthropic
from noeta.builtins.providers.impl.openai_compat import _assistant_message_to_openai
from noeta.builtins.providers.impl.openai_responses import (
    _assistant_message_to_responses,
)
from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.messages import Message, TextBlock, ThinkingBlock

_DEEPSEEK = ThinkingBlock(text="ds reasoning", provider="openai_compat")
_CLAUDE = ThinkingBlock(text="claude reasoning", signature="claude-sig", provider="anthropic")
_RESPONSES = ThinkingBlock(text="gpt reasoning", signature="gAAAA", provider="openai_responses")
_LEGACY = ThinkingBlock(text="old reasoning", signature="old-sig")

_ASSISTANT = Message(
    role="assistant",
    content=[_DEEPSEEK, _CLAUDE, _RESPONSES, _LEGACY, TextBlock(text="a")],
)


def test_anthropic_sends_only_its_own_and_legacy_thinking() -> None:
    wire = _assistant_message_to_anthropic(_ASSISTANT, None)
    thinking = [b["thinking"] for b in wire["content"] if b["type"] == "thinking"]
    assert thinking == ["claude reasoning", "old reasoning"]


def test_openai_compat_echoes_only_its_own_and_legacy_reasoning() -> None:
    wire = _assistant_message_to_openai(_ASSISTANT, "chat")
    assert wire["reasoning_content"] == "ds reasoning\nold reasoning"
    assert wire["encrypted_reasoning"] == "old-sig"


def test_responses_echoes_only_its_own_and_legacy_reasoning() -> None:
    items = _assistant_message_to_responses(_ASSISTANT, "responses", None)
    ciphertexts = [i.get("encrypted_content") for i in items if i["type"] == "reasoning"]
    assert ciphertexts == ["gAAAA", "old-sig"]


def test_provider_is_omitted_from_the_canonical_bytes_when_unknown() -> None:
    """Blocks recorded before the field existed keep their bytes, and decode
    as unknown provenance."""
    legacy_bytes = to_canonical_bytes(_LEGACY)
    assert b"provider" not in legacy_bytes
    assert from_canonical_bytes(legacy_bytes) == _LEGACY
    assert from_canonical_bytes(to_canonical_bytes(_CLAUDE)) == _CLAUDE
