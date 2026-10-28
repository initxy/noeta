"""How a ``tools/call`` result reaches the model."""

from __future__ import annotations

import base64
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


def texts(out: Any) -> list[str]:
    return [b.text for b in out.content if not isinstance(b, Image)]


async def test_blocks_pass_through_in_order(tmp_path: Path) -> None:
    real = base64.b64encode(png(2, 2)).decode()
    raw = base64.b64encode(b"png").decode()
    result: dict[str, Any] = {"content": [
        {"type": "text", "text": "a"},
        {"type": "resource", "resource": {"uri": "x", "text": "embedded"}},
        {"type": "resource_link", "uri": "file:///y", "name": "y", "mimeType": "text/plain"},
        {"type": "image", "mimeType": "image/png", "data": real},
        {"type": "image", "mimeType": "image/tiff", "data": raw},
        {"type": "image", "mimeType": "image/png", "data": "!!"},
    ], "structuredContent": {"k": 1}}
    out = await render_result(result, ctx(tmp_path))
    assert [type(b).__name__ for b in out.content] == ["Text", "Text", "Text", "Image", "Text",
                                                       "Text"]
    assert texts(out) == [
        "a", "embedded", "[resource link: y <file:///y> (text/plain)]",
        "[image (image/tiff, 3 bytes): type not shown inline]",
        "[image (image/png): data is not valid base64, not shown]"]
    assert not out.is_error and out.meta["structured"] is True


async def test_a_structured_only_result_is_its_json(tmp_path: Path) -> None:
    out = await render_result({"content": [], "structuredContent": {"b": 1, "a": 2}},
                              ctx(tmp_path))
    assert texts(out) == ['{"a": 2, "b": 1}']


async def test_a_text_over_25000_tokens_is_saved_and_only_pointed_to(
        tmp_path: Path) -> None:
    text = "x" * 100_004 + "\ny"  # 25,000 tokens are 100,000 characters
    out = await render_result({"content": [{"type": "text", "text": text}], "isError": True},
                              ctx(tmp_path))
    [note] = texts(out)
    assert note.startswith("The result (100,006 characters in 2 lines) is over the 25000-token "
                           "limit for an MCP result (NOETA_MAX_MCP_OUTPUT_TOKENS), so it was "
                           "saved to ")
    saved = note.split("saved to ", 1)[1].split(" instead of shown", 1)[0]
    assert Path(saved).read_text() == text
    assert "Format: plain text." in note and "x" * 100 not in note
    assert out.is_error
    fits = await render_result({"content": [{"type": "text", "text": "x" * 100_000}]},
                               ctx(tmp_path))
    assert texts(fits) == ["x" * 100_000]
    structured = await render_result({"content": [], "structuredContent": {"k": "v" * 100_000}},
                                     ctx(tmp_path))
    assert "Format: JSON." in texts(structured)[0]


async def test_without_a_spill_directory_the_head_is_kept_and_marked_at_the_end(
        tmp_path: Path) -> None:
    out = await render_result({"content": [{"type": "text", "text": "x" * 100_005}]},
                              ctx(tmp_path, spill=False))
    cut, note = texts(out)
    assert cut == "x" * 100_000
    assert note.startswith("[output truncated: over the 25000 token limit for an MCP result "
                           "(NOETA_MAX_MCP_OUTPUT_TOKENS); the server returned 100005 characters "
                           "and only the first 100000 are shown above.")


async def test_the_budget_follows_max_mcp_output_tokens(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_MAX_MCP_OUTPUT_TOKENS", "2000")
    real = base64.b64encode(png(2, 2)).decode()
    out = await render_result({"content": [
        {"type": "text", "text": "y" * 2000},
        {"type": "image", "mimeType": "image/png", "data": real},
        {"type": "image", "mimeType": "image/png", "data": real},
    ]}, ctx(tmp_path))
    # One image (1,600 tokens) fits in 2,000; the text gets the 400 tokens left.
    assert sum(isinstance(b, Image) for b in out.content) == 1
    # A result with images is cut, not saved.
    left_out, ys, note = texts(out)
    assert note.startswith("[output truncated: over the 2000 token limit")
    assert left_out == "[1 image(s) left out: over the tool output limit]"
    assert ys.startswith("yyy")
    assert not (tmp_path / "spill").exists()
    monkeypatch.setenv("NOETA_MAX_MCP_OUTPUT_TOKENS", "nonsense")
    assert max_output_tokens() == 25_000
