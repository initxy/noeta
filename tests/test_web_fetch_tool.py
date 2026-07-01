"""`webfetch` (phase two): fetch a public URL, render to Markdown.

The tool is driven through a fake fetch transport (no live network); the real
``HttpFetchTransport`` is exercised through ``httpx.MockTransport``. Every
``ToolResult.output`` is checked against ``runtime.tool._encode_output`` for the
B1 invariant (no raw ``ContentRef`` leaked inline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import _encode_output
from noeta.storage.memory import InMemoryContentStore
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES
from noeta.tools.web import (
    HttpFetchTransport,
    WebFetchTool,
    build_web_tools,
)
from noeta.tools.web.fetch import html_to_markdown


_PAGE = (
    "<html><head><title>Cats &amp; Kittens</title>"
    "<style>.x{color:red}</style></head>"
    "<body><script>track()</script>"
    "<h1>About cats</h1>"
    "<p>Kittens are <a href='https://example.com/cute'>cute</a>.</p>"
    "<ul><li>soft</li><li>small</li></ul>"
    "</body></html>"
)


@dataclass
class FakeFetchTransport:
    """In-memory url → page transport; raises for urls in ``raise_for``."""

    pages_by_url: dict[str, str] = field(default_factory=dict)
    raise_for: frozenset[str] = frozenset()
    error: Exception | None = None

    def fetch(self, url: str) -> str:
        if url in self.raise_for:
            raise self.error or RuntimeError(f"transport refused {url}")
        return self.pages_by_url.get(url, "")


def _ctx() -> tuple[ToolContext, InMemoryContentStore]:
    store = InMemoryContentStore()
    return ToolContext(artifact_store=store), store


def _assert_output_json_safe(result: ToolResult) -> None:
    _encode_output(result.output)


# ---------------------------------------------------------------------------
# tool identity
# ---------------------------------------------------------------------------


def test_webfetch_identity_low_risk() -> None:
    tool = WebFetchTool(transport=FakeFetchTransport())
    assert tool.name == "webfetch"
    assert tool.risk_level == "low"
    assert tool.description.strip()


def test_build_web_tools_exposes_webfetch() -> None:
    tools = build_web_tools()
    assert set(tools) == {"webfetch"}
    assert tools["webfetch"].risk_level == "low"


# ---------------------------------------------------------------------------
# happy path: fetch → markdown → artifact
# ---------------------------------------------------------------------------


def test_webfetch_renders_markdown_and_offloads() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx, store = _ctx()

    result = tool.invoke({"url": "https://x"}, ctx)
    assert result.success is True
    assert result.output["url"] == "https://x"
    assert result.output["title"] == "Cats & Kittens"  # entity unescaped
    md = result.output["content"]
    # heading marker, link rendered as markdown, list items as bullets.
    assert "# About cats" in md
    assert "[cute](https://example.com/cute)" in md
    assert "- soft" in md
    assert "- small" in md
    # script / style content is gone.
    assert "track()" not in md
    assert "color:red" not in md
    _assert_output_json_safe(result)

    # full markdown is the artifact; content_ref points at it.
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert result.output["content_ref"]["hash"] == ref.hash
    assert store.get(ref).decode("utf-8") == md
    assert result.output["content_ref"]["media_type"] == "text/markdown"


def test_webfetch_deterministic_same_bytes_same_artifact() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx_a, _ = _ctx()
    ctx_b, _ = _ctx()
    a = tool.invoke({"url": "https://x"}, ctx_a)
    b = tool.invoke({"url": "https://x"}, ctx_b)
    # Resume relies on identical input bytes → identical artifact.
    assert a.artifacts[0].hash == b.artifacts[0].hash
    assert a.output["content"] == b.output["content"]


# ---------------------------------------------------------------------------
# bad input + transport / auth failures degrade cleanly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["", "   ", None, 5])
def test_webfetch_rejects_bad_url(bad_url: Any) -> None:
    tool = WebFetchTool(transport=FakeFetchTransport())
    ctx, _ = _ctx()
    result = tool.invoke({"url": bad_url}, ctx)
    assert result.success is False
    _assert_output_json_safe(result)


def test_webfetch_degrades_on_transport_failure() -> None:
    transport = FakeFetchTransport(raise_for=frozenset({"https://boom"}))
    ctx, _ = _ctx()
    result = WebFetchTool(transport=transport).invoke({"url": "https://boom"}, ctx)
    assert result.success is False
    assert "webfetch failed" in result.summary
    _assert_output_json_safe(result)


def test_webfetch_private_url_failure_names_the_cause() -> None:
    # A private / authenticated URL answers 401/403; httpx raises and the tool
    # surfaces a message that names the cause (the limitation in the description).
    err = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=httpx.Request("GET", "https://private/secret"),
        response=httpx.Response(401),
    )
    transport = FakeFetchTransport(
        raise_for=frozenset({"https://private/secret"}), error=err
    )
    ctx, _ = _ctx()
    result = WebFetchTool(transport=transport).invoke(
        {"url": "https://private/secret"}, ctx
    )
    assert result.success is False
    assert "401" in result.summary or "Unauthorized" in result.summary


# ---------------------------------------------------------------------------
# large page → inline content shrinks, full body stays in the artifact
# ---------------------------------------------------------------------------


def test_webfetch_large_page_truncates_inline_keeps_full_artifact() -> None:
    big = "<html><body>" + ("<p>word here</p>" * 12000) + "</body></html>"
    transport = FakeFetchTransport(pages_by_url={"https://big": big})
    ctx, store = _ctx()
    result = WebFetchTool(transport=transport).invoke({"url": "https://big"}, ctx)
    assert result.success is True
    assert result.output["truncated"] is True
    # inline output respects the canonical byte ceiling ...
    assert _encode_output(result.output)
    from noeta.tools._limits import encoded_len

    assert encoded_len(result.output) <= INLINE_CONTENT_MAX_BYTES
    # ... but the full markdown survives in the artifact, longer than inline.
    ref = result.artifacts[0]
    assert len(store.get(ref)) > len(result.output["content"].encode("utf-8"))


# ---------------------------------------------------------------------------
# real HttpFetchTransport over httpx.MockTransport (no live network)
# ---------------------------------------------------------------------------


def test_http_fetch_transport_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(200, text=_PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpFetchTransport(client=client)
    text = transport.fetch("https://example.com/page")
    assert "About cats" in text


def test_http_fetch_transport_raises_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpFetchTransport(client=client)
    with pytest.raises(httpx.HTTPStatusError):
        transport.fetch("https://private.example.com/secret")


# ---------------------------------------------------------------------------
# html_to_markdown helper — deterministic, structure-aware
# ---------------------------------------------------------------------------


def test_html_to_markdown_basic_structure() -> None:
    md = html_to_markdown(
        "<h2>Title</h2><p>hello <a href='/x'>link</a></p>"
    )
    assert "## Title" in md
    assert "[link](/x)" in md
