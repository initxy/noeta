"""noeta.sdk image-input write side — ``Client.put_content`` + facade exports.

The write-side mirror of ``get_content``: a product backend that only imports
``noeta.sdk`` (the D2 weld) can store raw bytes (e.g. a decoded base64 image),
get back a ``ContentRef``, wrap it in an ``ImageBlock`` for a user turn, and
later deref the same bytes by hash. Content-addressed: identical bytes →
identical SHA-256 hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Client, ContentRef, ImageBlock, Options
from noeta.testing.fake_llm import FakeLLMProvider


def _client(workspace: Path) -> Client:
    return Client(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        workspace_dir=workspace,
        multi_turn=True,
    )


def test_facade_exports_image_block_and_content_ref() -> None:
    # The two types a thin backend needs to build a user-turn image without
    # touching noeta.protocols.
    assert ImageBlock is not None
    assert ContentRef is not None


def test_put_content_roundtrip_and_hash(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = b"\x89PNG\r\n\x1a\n fake image bytes"

    ref = client.put_content(body, media_type="image/png")

    assert isinstance(ref, ContentRef)
    assert ref.media_type == "image/png"
    assert ref.size == len(body)
    # Same hash a backend / frontend would compute over the raw bytes.
    assert ref.hash == hashlib.sha256(body).hexdigest()
    # Write side and read side agree on the same blob.
    assert client.get_content(ref.hash) == body


def test_put_content_is_content_addressed(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = b"identical bytes"

    ref1 = client.put_content(body, media_type="image/jpeg")
    ref2 = client.put_content(body, media_type="image/jpeg")

    assert ref1.hash == ref2.hash  # dedup by content
    # A ContentRef wraps cleanly into a user-turn ImageBlock.
    block = ImageBlock(source=ref1)
    assert block.source.hash == ref1.hash
