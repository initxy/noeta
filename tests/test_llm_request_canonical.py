"""Unset optional LLMRequest fields must stay out of the canonical bytes.

``__canonical_omit_none__`` keeps ``output_schema`` / ``thinking`` /
``effort`` out of the serialized request when the caller left them unset,
so a request that ignores them hashes identically to the byte shape every
recording and golden pins. An unset field leaking in — even as ``null`` —
invalidates that whole library at once, so the exact bytes are asserted
here. When this assertion breaks, check in order:

1. Is ``__canonical_omit_none__`` declared on ``LLMRequest``?
2. Did ``to_canonical`` semantics change in ``canonical.py``?
3. Does some other optional field lack its omit_none frozenset entry?
"""

from __future__ import annotations

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import LLMRequest, Message, TextBlock


def test_llmrequest_default_new_fields_omitted_canonical_bytes_pinned() -> None:
    """Unset output_schema/thinking/effort stay out of the canonical bytes.

    Every other field is set explicitly — model/messages/tools/temperature/
    max_tokens/metadata, plus ``system`` as a real Message — so the pinned
    bytes cover a fully populated request and the only thing missing is the
    three omit_none fields, which must not appear even as ``null``.
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
    # Golden bytes — the exact wire shape recordings and replay comparisons
    # are pinned against.
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
    """Set the three fields → they must appear; omit_none must not swallow a value the caller supplied."""
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
