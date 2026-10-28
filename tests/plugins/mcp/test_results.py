"""How a ``tools/call`` result reaches the model."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.tool import ToolContext
from noeta.core.types import Image
from noeta.plugins.mcp.results import INLINE_MAX_BYTES, render_result


def ctx(tmp_path: Path, spill: bool = True) -> ToolContext:
    return ToolContext(task_id="t", call_id="c1", env=LocalExecEnv(str(tmp_path)), state={},
                       record=lambda *_: None,
                       spill_dir=str(tmp_path / "spill") if spill else None)


async def test_blocks_are_rendered_in_order(tmp_path: Path) -> None:
    png = base64.b64encode(b"png").decode()
    result: dict[str, Any] = {"content": [
        {"type": "text", "text": "a"},
        {"type": "resource", "resource": {"uri": "x", "text": "embedded"}},
        {"type": "resource_link", "uri": "file:///y", "name": "y", "mimeType": "text/plain"},
        {"type": "image", "mimeType": "image/png", "data": png},
        {"type": "image", "mimeType": "image/tiff", "data": png},
        {"type": "image", "mimeType": "image/png", "data": "!!"},
    ], "structuredContent": {"k": 1}}
    out = await render_result(result, ctx(tmp_path))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    assert body == {
        "text": "a\nembedded\n[resource link: y <file:///y> (text/plain)]\n"
                "[image (image/tiff, 3 bytes): type not shown inline]\n"
                "[image (image/png): data is not valid base64, not shown]",
        "non_text_blocks": 5,
        "structured_content": {"k": 1},
    }
    assert [type(b) for b in out.content[1:]] == [Image]
    assert not out.is_error


async def test_a_huge_text_is_cut_to_a_marked_head_and_saved(tmp_path: Path) -> None:
    text = "x" * (INLINE_MAX_BYTES + 5)
    out = await render_result({"content": [{"type": "text", "text": text}], "isError": True},
                              ctx(tmp_path))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    assert body["text"].startswith(
        f"[truncated: the server returned {len(text)} bytes; what follows is the first "
        f"{INLINE_MAX_BYTES // 2}]\nxxx")
    assert Path(body["text_saved_to"]).read_text() == text
    assert out.is_error


async def test_a_huge_structured_result_is_saved_not_inlined(tmp_path: Path) -> None:
    big = {"rows": ["y" * 1000] * 600}
    out = await render_result({"content": [], "structuredContent": big}, ctx(tmp_path, False))
    body = json.loads(out.content[0].text)  # type: ignore[union-attr]
    assert "structured_content" not in body and body["structured_content_bytes"] > 500_000
