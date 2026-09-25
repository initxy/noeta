"""MCP list calls follow ``nextCursor`` to the last page.

A server that pages its ``tools/list`` / ``prompts/list`` /
``resources/list`` used to lose every tool after the first page; both
transports now walk the cursor, bounded so a server that never stops handing
one back cannot loop the build forever.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from noeta.builtins.mcp.impl import McpHttpClient, build_mcp_tools
from noeta.builtins.mcp.impl._client import MAX_LIST_PAGES
from noeta.runtime.mcp import McpServerSpec

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def test_stdio_tools_list_follows_next_cursor() -> None:
    spec = McpServerSpec(alias="fake", argv=(sys.executable, "-u", _FAKE, "page"))
    tools, clients, _ = build_mcp_tools((spec,))
    try:
        assert sorted(tools) == ["mcp__fake__alpha", "mcp__fake__beta"]
    finally:
        for c in clients:
            c.shutdown()


class _PagedHttp:
    """An ``HttpPostFn`` whose three list methods answer in two pages."""

    def __init__(self, *, endless: bool = False) -> None:
        self.endless = endless
        self.cursors: list[Any] = []

    def __call__(self, req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
        method = req["method"]
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2024-11-05", "capabilities": {}}
        else:
            key = method.split("/")[0]
            cursor = (req.get("params") or {}).get("cursor")
            self.cursors.append(cursor)
            entry = (lambda n: {"uri": f"file:///{n}", "name": n}) if key == "resources" else (
                lambda n: {"name": n}
            )
            if self.endless:
                n = len(self.cursors)
                result = {key: [entry(f"e{n}")], "nextCursor": f"c{n}"}
            elif cursor is None:
                result = {key: [entry("one")], "nextCursor": "c2"}
            else:
                result = {key: [entry("two")]}
        return json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()


def test_http_list_calls_follow_next_cursor() -> None:
    post = _PagedHttp()
    c = McpHttpClient(url="http://127.0.0.1/mcp", post=post)
    c.start()
    assert [t["name"] for t in c.list_tools()] == ["one", "two"]
    assert [p["name"] for p in c.list_prompts()] == ["one", "two"]
    assert [r["name"] for r in c.list_resources()] == ["one", "two"]
    assert post.cursors == [None, "c2"] * 3


def test_endless_cursor_is_bounded() -> None:
    post = _PagedHttp(endless=True)
    c = McpHttpClient(url="http://127.0.0.1/mcp", post=post)
    c.start()
    assert len(c.list_tools()) == MAX_LIST_PAGES
