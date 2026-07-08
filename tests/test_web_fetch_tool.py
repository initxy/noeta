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
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.web import (
    ContainerCurlFetchTransport,
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


@dataclass
class FakeExecEnv:
    """Minimal ``ExecEnv`` stand-in: only ``run_argv`` behaves (sandbox path).

    Records every argv it is handed and returns a scripted ``_RunOutcome`` so a
    container-transport test never shells out. Other ``ExecEnv`` methods are
    unused by the web transports and left unimplemented.
    """

    stdout: bytes = b""
    returncode: int = 0
    stderr: bytes = b""
    timed_out: bool = False
    calls: list[list[str]] = field(default_factory=list)
    last_cwd: Any = None
    last_timeout_s: int = 0
    last_output_cap: int = 0

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None):
        self.calls.append(list(argv))
        self.last_cwd = cwd
        self.last_timeout_s = timeout_s
        self.last_output_cap = output_cap
        return _RunOutcome(
            returncode=self.returncode,
            duration_ms=1,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=self.timed_out,
        )


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


def test_http_fetch_transport_aborts_oversize_body() -> None:
    # A body larger than ``max_bytes`` is refused mid-stream rather than
    # buffered whole (unbounded memory + regex CPU DoS otherwise).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpFetchTransport(client=client, max_bytes=1024)
    with pytest.raises(ValueError, match="exceeds 1024 byte limit"):
        transport.fetch("https://example.com/huge")


def test_http_fetch_transport_oversize_degrades_to_failed_result() -> None:
    # End-to-end: the WebFetchTool catches the cap error and degrades to a
    # failed ToolResult instead of crashing the step.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tool = WebFetchTool(transport=HttpFetchTransport(client=client, max_bytes=1024))
    ctx, _ = _ctx()
    result = tool.invoke({"url": "https://example.com/huge"}, ctx)
    assert result.success is False
    assert "byte limit" in result.summary


# ---------------------------------------------------------------------------
# html_to_markdown helper — deterministic, structure-aware
# ---------------------------------------------------------------------------


def test_html_to_markdown_basic_structure() -> None:
    md = html_to_markdown(
        "<h2>Title</h2><p>hello <a href='/x'>link</a></p>"
    )
    assert "## Title" in md
    assert "[link](/x)" in md


# ---------------------------------------------------------------------------
# sandbox path: build_web_tools(exec_env=) egresses through the container
# ---------------------------------------------------------------------------


def test_build_web_tools_sandbox_uses_container_fetch_transport() -> None:
    fake = FakeExecEnv(stdout=_PAGE.encode("utf-8"))
    tools = build_web_tools(exec_env=fake)
    assert set(tools) == {"webfetch"}
    assert isinstance(tools["webfetch"].transport, ContainerCurlFetchTransport)


def test_container_fetch_runs_curl_and_renders_markdown() -> None:
    fake = FakeExecEnv(stdout=_PAGE.encode("utf-8"))
    tool = build_web_tools(exec_env=fake)["webfetch"]
    ctx, store = _ctx()

    result = tool.invoke({"url": "https://x"}, ctx)
    assert result.success is True
    # the request went out as `curl ... <url>` inside the container
    assert fake.calls, "run_argv was not invoked"
    argv = fake.calls[0]
    assert argv[0] == "curl"
    assert argv[-1] == "https://x"
    assert "-A" in argv  # user-agent forwarded
    # P2a: --fail makes a 4xx/5xx a nonzero exit (parity with httpx
    # raise_for_status) instead of returning the error page as a success body.
    assert "--fail" in argv
    # the scripted HTML is rendered by the SAME html_to_markdown as the httpx path
    md = result.output["content"]
    assert "# About cats" in md
    assert "[cute](https://example.com/cute)" in md
    assert "- soft" in md
    assert result.output["title"] == "Cats & Kittens"
    _assert_output_json_safe(result)


def test_container_fetch_nonzero_exit_degrades() -> None:
    # A private / authenticated URL: curl exits nonzero and the tool degrades to
    # ToolResult(success=False), exactly like the httpx 401/403 path.
    fake = FakeExecEnv(
        stdout=b"", returncode=22, stderr=b"curl: (22) 403 Forbidden"
    )
    tool = build_web_tools(exec_env=fake)["webfetch"]
    ctx, _ = _ctx()
    result = tool.invoke({"url": "https://private"}, ctx)
    assert result.success is False
    assert "webfetch failed" in result.summary
    assert "403" in result.summary
    _assert_output_json_safe(result)


def test_container_fetch_timeout_degrades() -> None:
    fake = FakeExecEnv(stdout=b"", returncode=-1, timed_out=True)
    tool = build_web_tools(exec_env=fake)["webfetch"]
    ctx, _ = _ctx()
    result = tool.invoke({"url": "https://slow"}, ctx)
    assert result.success is False
    assert "webfetch failed" in result.summary
