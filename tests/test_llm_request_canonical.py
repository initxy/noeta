"""LLMRequest new optional fields + canonical byte-omit regression guard.

Three optional fields (``output_schema``/``thinking``/``effort``) were added
to :class:`LLMRequest` AFTER a large library of recordings and golden
canary tests already pinned the canonical byte shape. The omit_none
mechanism (``__canonical_omit_none__``) guarantees that a request built
*without* these fields serializes byte-for-byte identical to the pre-
addition shape — the whole point of the frozenset declaration. This test
pins that guarantee with the exact bytes observed *before* the fields
were added. If this assertion ever fails, re-check:

1. Did ``__canonical_omit_none__`` on ``LLMRequest`` get removed or
   renamed?
2. Did ``to_canonical`` semantics change in ``canonical.py``?
3. Was a DIFFERENT new optional field added WITHOUT the omit_none
   frozenset entry?
"""

from __future__ import annotations

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import LLMRequest, Message, TextBlock


def test_llmrequest_default_new_fields_omitted_canonical_bytes_pinned() -> None:
    """Default output_schema/thinking/effort stay out of canonical bytes — bytes pinned.

    Build an LLMRequest that sets every field EXCEPT the three new ones:
    model/messages/tools/temperature/max_tokens/metadata explicit, system as
    an explicit Message. The canonical bytes must match the pre-three-fields
    shape exactly — the new fields never appear in the byte stream, not even
    as ``null``.
    """
    req = LLMRequest(
        model="claude-sonnet-4-20250514",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        system=Message(
            role="system", content=[TextBlock(text="you are helpful")]
        ),
        temperature=0.7,
        max_tokens=1024,
        metadata={"k": "v"},
    )
    body = to_canonical_bytes(req)
    # Golden bytes captured BEFORE output_schema / thinking / effort were
    # added to LLMRequest (2026-06-11 baseline).
    assert body == (
        b'{"max_tokens":1024,'
        b'"messages":[{"__canonical_tag__":"message",'
        b'"content":[{"__canonical_tag__":"text_block","text":"hi"}],'
        b'"role":"user"}],'
        b'"metadata":{"k":"v"},'
        b'"model":"claude-sonnet-4-20250514",'
        b'"system":{"__canonical_tag__":"message",'
        b'"content":[{"__canonical_tag__":"text_block","text":"you are helpful"}],'
        b'"role":"system"},'
        b'"temperature":0.7,'
        b'"tools":[]}'
    )


def test_llmrequest_set_fields_appear_in_canonical() -> None:
    """Set the three new fields → they must appear in canonical bytes (else the feature is dead)."""
    req = LLMRequest(
        model="m",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        output_schema={"type": "object"},
        thinking="adaptive",
        effort="high",
    )
    text = to_canonical_bytes(req).decode("utf-8")
    assert '"output_schema":' in text
    assert '"thinking":' in text
    assert '"effort":' in text
    assert '"adaptive"' in text
    assert '"high"' in text
