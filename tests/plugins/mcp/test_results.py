"""How a ``tools/call`` result reaches the model."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.tool import ToolContext
from noeta.core.types import Image
from noeta.plugins.mcp.results import max_output_tokens, render_result
from tests.images import png


def ctx(tmp_path: Path, spill: bool = True) -> ToolContext:
    return ToolContext(task_id="t", call_id="c1", env=LocalExecEnv(str(tmp_path)), state={},
                       record=lambda *_: None,
                       spill_dir=str(tmp_path / "spill") if spill else None)


async def test_blocks_are_rendered_in_order(tmp_path: Path) -> None:
    real = base64.b64encode(png(2, 2)).decode()
    raw = base64.b64encode(b"png").decode()
    result: dict[str, Any] = {"content": [
        {"type": "text", "text": "a"},
        {"type": "resource", "resource": {"uri": "x", "text": "embedded"}},
        {"type": "resource_link", "uri": "file:///y", "name": "y", "mimeType": "text/plain"},
        {"type": "image", "mimeType": "image/png", "data": real},
        {"type": "image", "mimeType": "image/tiff", "data": raw},
        {"type": "image", "mimeType": "image/png", "data": "!!"},
        {"type": "image", "mimeType": "image/png", "data": raw},
    ], "structuredContent": {"k": 1}}
    out = await render_result(result, ctx(tmp_path))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    assert body["text"].startswith(
        "a\nembedded\n[resource link: y <file:///y> (text/plain)]\n"
        "[image (image/tiff, 3 bytes): type not shown inline]\n"
        "[image (image/png): data is not valid base64, not shown]\n"
        "[image (image/png, 3 bytes): not shown: ")
    assert body == {
        "text": body["text"],
        "non_text_blocks": 6,
        "structured_content": {"k": 1},
    }
    assert [type(b) for b in out.content[1:]] == [Image]
    assert not out.is_error


async def test_a_text_over_25000_tokens_is_cut_to_a_marked_head_and_saved(
        tmp_path: Path) -> None:
    text = "x" * 100_005  # 25,000 tokens are 100,000 characters
    out = await render_result({"content": [{"type": "text", "text": text}], "isError": True},
                              ctx(tmp_path))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    head, cut = body["text"].split("\n", 1)
    assert head.startswith("[output truncated: over the 25000 token limit for an MCP result "
                           "(MAX_MCP_OUTPUT_TOKENS); the server returned 100005 characters, "
                           "what follows is the first 100000.")
    assert cut == "x" * 100_000
    assert Path(body["text_saved_to"]).read_text() == text
    assert out.is_error
    fits = await render_result({"content": [{"type": "text", "text": "x" * 100_000}]},
                               ctx(tmp_path))
    assert json.loads(fits.content[0].text)["text"] == "x" * 100_000  # type: ignore[union-attr]


async def test_the_budget_follows_max_mcp_output_tokens(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "2000")
    real = base64.b64encode(png(2, 2)).decode()
    out = await render_result({"content": [
        {"type": "text", "text": "y" * 2000},
        {"type": "image", "mimeType": "image/png", "data": real},
        {"type": "image", "mimeType": "image/png", "data": real},
    ]}, ctx(tmp_path))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    # One image (1,600 tokens) fits in 2,000; the text gets the 400 tokens left.
    assert [type(b) for b in out.content[1:]] == [Image]
    assert body["text"].startswith("[output truncated: over the 2000 token limit")
    assert "\n[1 image(s) left out: over the tool output limit]\nyyy" in body["text"]
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "nonsense")
    assert max_output_tokens() == 25_000


async def test_a_huge_structured_result_is_saved_not_inlined(tmp_path: Path) -> None:
    big = {"rows": ["y" * 1000] * 60}
    out = await render_result({"content": [], "structuredContent": big}, ctx(tmp_path, False))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    assert "structured_content" not in body and body["structured_content_bytes"] > 50_000
