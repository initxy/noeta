"""MCP tool results keep more than text blocks.

``structuredContent`` rides in the output, an inline-able image becomes a
``ToolResult.images`` ref the model can see, and audio / resource links /
binary resources are described in text lines instead of silently dropped.
"""

from __future__ import annotations

import base64
from typing import Any

from noeta.builtins.mcp.impl.tool import _result_to_tool_result
from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.limits import INLINE_CONTENT_MAX_BYTES

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _map(result: dict[str, Any]) -> tuple[Any, InMemoryContentStore]:
    store = InMemoryContentStore()
    return _result_to_tool_result("mcp__x__t", result, ToolContext(artifact_store=store)), store


def test_structured_content_is_included() -> None:
    res, _ = _map({"content": [{"type": "text", "text": "hi"}], "structuredContent": {"n": 3}})
    assert res.output["structured_content"] == {"n": 3}
    assert res.output["text"] == "hi"
    assert "structured content" in res.summary


def test_large_structured_content_is_offloaded() -> None:
    big = {"blob": "y" * INLINE_CONTENT_MAX_BYTES}
    res, _ = _map({"content": [], "structuredContent": big})
    assert "structured_content" not in res.output
    assert res.output["structured_content_ref"] and res.artifacts


def test_image_becomes_tool_result_image() -> None:
    data = base64.b64encode(_PNG).decode()
    res, store = _map({"content": [{"type": "image", "data": data, "mimeType": "image/png"}]})
    (ref,) = res.images
    assert store.get(ref) == _PNG
    assert res.output["non_text_blocks"] == 1
    assert "1 image(s)" in res.summary


def test_unusable_images_are_described() -> None:
    res, _ = _map(
        {
            "content": [
                {"type": "image", "data": "!!!", "mimeType": "image/png"},
                {"type": "image", "data": base64.b64encode(b"x").decode(), "mimeType": "image/tiff"},
            ]
        }
    )
    assert res.images == []
    assert "not valid base64" in res.output["text"]
    assert "image/tiff" in res.output["text"]


def test_links_audio_and_resources_are_listed_in_order() -> None:
    res, _ = _map(
        {
            "content": [
                {"type": "text", "text": "before"},
                {"type": "resource_link", "uri": "file:///a.txt", "name": "a", "mimeType": "text/plain"},
                {"type": "audio", "data": base64.b64encode(b"abc").decode(), "mimeType": "audio/wav"},
                {"type": "resource", "resource": {"uri": "file:///b", "text": "embedded body"}},
                {"type": "resource", "resource": {"uri": "file:///c", "blob": "AA==", "mimeType": "application/pdf"}},
            ]
        }
    )
    lines = res.output["text"].splitlines()
    assert lines[0] == "before"
    assert lines[1].startswith("[resource link: a <file:///a.txt>")
    assert lines[2] == "[audio (audio/wav, 3 bytes): not shown]"
    assert lines[3] == "embedded body"
    assert "file:///c" in lines[4] and "application/pdf" in lines[4]
    assert res.output["non_text_blocks"] == 4
