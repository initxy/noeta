"""``webfetch`` — fetch a URL, render it to Markdown, answer ``prompt`` on it.

Nothing here touches the network: the tool runs against a fake transport, and
the real ``HttpFetchTransport`` is driven through ``httpx.MockTransport``. The
digest path runs against a scripted provider / fake digester. Every
``ToolResult.output`` is re-encoded through ``runtime.tool._encode_output`` to
prove no raw ``ContentRef`` leaks inline — the model must only ever see refs the
adapter knows how to serialise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from noeta.protocols.messages import LLMResponse, TextBlock
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import _encode_output
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore
from noeta.runtime.subproc import RunOutcome
from noeta.builtins.web.impl import (
    ContainerCurlFetchTransport,
    CrossHostRedirect,
    HttpFetchTransport,
    LLMPageDigester,
    WebFetchTool,
    build_web_session_pack,
    build_web_tools,
)
from noeta.builtins.web.impl.fetch import html_to_markdown
from noeta.execution.session_pack import SessionBuildContext


_PAGE = (
    "<html><head><title>Cats &amp; Kittens</title>"
    "<style>.x{color:red}</style></head>"
    "<body><script>track()</script>"
    "<h1>About cats</h1>"
    "<p>Kittens are <a href='https://example.com/cute'>cute</a>.</p>"
    "<ul><li>soft</li><li>small</li></ul>"
    "</body></html>"
)

#: The stderr the container curl emits on a plain 200 (no redirect).
_CURL_META_OK = b"__noeta_webfetch_meta__ 200 \n"


def _args(url: str, prompt: str = "What is this page about?") -> dict[str, Any]:
    return {"url": url, "prompt": prompt}


@dataclass
class FakeFetchTransport:
    """In-memory url → page transport; raises for urls in ``raise_for``."""

    pages_by_url: dict[str, str] = field(default_factory=dict)
    raise_for: frozenset[str] = frozenset()
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        if url in self.raise_for:
            raise self.error or RuntimeError(f"transport refused {url}")
        return self.pages_by_url.get(url, "")


@dataclass
class FakeDigester:
    """Scripted ``PageDigester``: records the call, replies or raises."""

    answer: str = "A page about cats."
    error: Exception | None = None
    calls: list[dict[str, str]] = field(default_factory=list)

    def digest(
        self, *, url: str, title: str, page_markdown: str, prompt: str
    ) -> str:
        self.calls.append(
            {
                "url": url,
                "title": title,
                "page_markdown": page_markdown,
                "prompt": prompt,
            }
        )
        if self.error is not None:
            raise self.error
        return self.answer


@dataclass
class FakeExecEnv:
    """Minimal ``ExecEnv`` stand-in: only ``run_argv`` behaves (sandbox path).

    Records every argv it is handed and returns scripted ``RunOutcome``s so a
    container-transport test never shells out: ``script`` outcomes are consumed
    first (one per call, for redirect-hop tests), then the flat fields repeat.
    Other ``ExecEnv`` methods are unused by the web transports and left
    unimplemented.
    """

    stdout: bytes = b""
    returncode: int = 0
    stderr: bytes = _CURL_META_OK
    timed_out: bool = False
    stdout_truncated: bool = False
    script: list[RunOutcome] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    last_cwd: Any = None
    last_timeout_s: int = 0
    last_output_cap: int = 0

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None):
        self.calls.append(list(argv))
        self.last_cwd = cwd
        self.last_timeout_s = timeout_s
        self.last_output_cap = output_cap
        if self.script:
            return self.script.pop(0)
        return RunOutcome(
            returncode=self.returncode,
            duration_ms=1,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=False,
            timed_out=self.timed_out,
        )


def _outcome(
    stdout: bytes = b"",
    stderr: bytes = _CURL_META_OK,
    returncode: int = 0,
    stdout_truncated: bool = False,
) -> RunOutcome:
    return RunOutcome(
        returncode=returncode,
        duration_ms=1,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=False,
        timed_out=False,
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
    assert tool.name == "WebFetch"
    assert tool.risk_level == "low"
    assert tool.description.strip()
    # Claude Code parity: url + prompt, both required.
    assert tool.input_schema["required"] == ["url", "prompt"]
    assert set(tool.input_schema["properties"]) == {"url", "prompt"}


def test_build_web_tools_exposes_webfetch() -> None:
    tools = build_web_tools()
    assert set(tools) == {"WebFetch"}
    assert tools["WebFetch"].risk_level == "low"


# ---------------------------------------------------------------------------
# happy path (no digester wired): fetch → markdown → artifact
# ---------------------------------------------------------------------------


def test_webfetch_renders_markdown_and_offloads() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx, store = _ctx()

    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is True
    md = result.output
    assert md.startswith("Title: Cats & Kittens\nURL: https://x\n\n")  # entity unescaped
    assert "# About cats" in md
    assert "[cute](https://example.com/cute)" in md
    assert "- soft" in md
    assert "- small" in md
    # Script and style bodies are stripped: they are pure token cost to a model
    # and a place for a page to smuggle instructions.
    assert "track()" not in md
    assert "color:red" not in md
    # No ref/hash rides the model-facing text; the artifact is audit-side.
    assert "content_ref" not in md
    _assert_output_json_safe(result)

    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert ref.media_type == "text/markdown"
    assert store.get(ref).decode("utf-8") in md


def test_webfetch_deterministic_same_bytes_same_artifact() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx_a, _ = _ctx()
    ctx_b, _ = _ctx()
    a = tool.invoke(_args("https://x"), ctx_a)
    b = tool.invoke(_args("https://x"), ctx_b)
    # Resume relies on identical input bytes → identical artifact.
    assert a.artifacts[0].hash == b.artifacts[0].hash
    assert a.output == b.output


# ---------------------------------------------------------------------------
# bad input + transport / auth failures degrade cleanly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", ["", "   ", None, 5])
def test_webfetch_rejects_bad_url(bad_url: Any) -> None:
    tool = WebFetchTool(transport=FakeFetchTransport())
    ctx, _ = _ctx()
    result = tool.invoke({"url": bad_url, "prompt": "summarize"}, ctx)
    assert result.success is False
    _assert_output_json_safe(result)


@pytest.mark.parametrize("bad_prompt", ["", "   ", None, 5])
def test_webfetch_rejects_bad_prompt(bad_prompt: Any) -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx, _ = _ctx()
    result = tool.invoke({"url": "https://x", "prompt": bad_prompt}, ctx)
    assert result.success is False
    assert "prompt" in result.summary
    assert transport.calls == []  # rejected before any fetch
    _assert_output_json_safe(result)


def test_webfetch_degrades_on_transport_failure() -> None:
    transport = FakeFetchTransport(raise_for=frozenset({"https://boom"}))
    ctx, _ = _ctx()
    result = WebFetchTool(transport=transport).invoke(_args("https://boom"), ctx)
    assert result.success is False
    assert "WebFetch failed" in result.summary
    _assert_output_json_safe(result)


def test_webfetch_empty_rendering_degrades_to_failure() -> None:
    # A body that renders to empty Markdown (blocked, empty, or script-only
    # page) must not report success: "fetched (0B markdown)" reads as "the page
    # had nothing on it" and stops the model from trying another source.
    empty = "<html><head><title>t</title></head><body><script>x()</script></body></html>"
    transport = FakeFetchTransport(pages_by_url={"https://hollow": empty})
    ctx, _ = _ctx()
    result = WebFetchTool(transport=transport).invoke(_args("https://hollow"), ctx)
    assert result.success is False
    assert "no readable text" in result.summary
    assert result.artifacts == []
    _assert_output_json_safe(result)


def test_webfetch_private_url_failure_names_the_cause() -> None:
    # A private / authenticated URL answers 401/403. The summary has to name the
    # cause, or the model retries the same fetch instead of asking for access.
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
        _args("https://private/secret"), ctx
    )
    assert result.success is False
    assert "401" in result.summary or "Unauthorized" in result.summary


# ---------------------------------------------------------------------------
# Claude Code parity: HTTP → HTTPS upgrade
# ---------------------------------------------------------------------------


def test_webfetch_upgrades_http_to_https() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx, _ = _ctx()
    result = tool.invoke(_args("http://x"), ctx)
    assert result.success is True
    assert transport.calls == ["https://x"]  # upgraded before the transport
    assert "URL: https://x" in result.output


# ---------------------------------------------------------------------------
# Claude Code parity: 15-minute per-URL cache (successes only)
# ---------------------------------------------------------------------------


def test_webfetch_caches_page_for_repeat_fetches() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    tool = WebFetchTool(transport=transport)
    ctx, _ = _ctx()
    first = tool.invoke(_args("https://x"), ctx)
    second = tool.invoke(_args("https://x", prompt="What color are kittens?"), ctx)
    assert first.success and second.success
    assert transport.calls == ["https://x"]  # one fetch serves both prompts
    assert second.artifacts[0].hash == first.artifacts[0].hash


def test_webfetch_cache_expires_after_ttl() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    now = [1000.0]
    tool = WebFetchTool(transport=transport, clock=lambda: now[0])
    ctx, _ = _ctx()
    tool.invoke(_args("https://x"), ctx)
    now[0] += 901.0  # past the 15-minute TTL
    tool.invoke(_args("https://x"), ctx)
    assert transport.calls == ["https://x", "https://x"]


def test_webfetch_failures_are_not_cached() -> None:
    # A transient failure must not become a 15-minute blind spot: the next
    # call re-fetches.
    transport = FakeFetchTransport(
        pages_by_url={"https://x": _PAGE}, raise_for=frozenset({"https://x"})
    )
    tool = WebFetchTool(transport=transport)
    ctx, _ = _ctx()
    assert tool.invoke(_args("https://x"), ctx).success is False
    transport.raise_for = frozenset()
    assert tool.invoke(_args("https://x"), ctx).success is True
    assert transport.calls == ["https://x", "https://x"]


# ---------------------------------------------------------------------------
# Claude Code parity: cross-host redirects returned, not followed
# ---------------------------------------------------------------------------


def test_webfetch_cross_host_redirect_returned_to_model() -> None:
    transport = FakeFetchTransport(
        raise_for=frozenset({"https://a.example/x"}),
        error=CrossHostRedirect("https://a.example/x", "https://b.example/y"),
    )
    ctx, _ = _ctx()
    result = WebFetchTool(transport=transport).invoke(
        _args("https://a.example/x"), ctx
    )
    assert result.success is True
    assert "https://b.example/y" in result.output
    assert "new WebFetch call" in result.output
    assert "different host" in result.summary
    assert result.artifacts == []
    _assert_output_json_safe(result)


# ---------------------------------------------------------------------------
# digest path: prompt answered by the injected digester
# ---------------------------------------------------------------------------


def test_webfetch_digest_answers_prompt_instead_of_raw_page() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    digester = FakeDigester(answer="Kittens are soft and small.")
    tool = WebFetchTool(transport=transport, digester=digester)
    ctx, store = _ctx()

    result = tool.invoke(_args("https://x", prompt="What are kittens like?"), ctx)
    assert result.success is True
    assert result.output == (
        "Title: Cats & Kittens\nURL: https://x\n\nKittens are soft and small."
    )
    # The raw rendering stays out of the model's context...
    assert "- soft" not in result.output
    assert "digested" in result.summary
    # ...but survives in full as the audit artifact.
    assert "- soft" in store.get(result.artifacts[0]).decode("utf-8")
    _assert_output_json_safe(result)

    # The digester saw the upgraded URL, the rendering, and the prompt.
    (call,) = digester.calls
    assert call["url"] == "https://x"
    assert call["prompt"] == "What are kittens like?"
    assert "# About cats" in call["page_markdown"]


def test_webfetch_digest_failure_falls_back_to_raw_render() -> None:
    transport = FakeFetchTransport(pages_by_url={"https://x": _PAGE})
    digester = FakeDigester(error=RuntimeError("provider down"))
    tool = WebFetchTool(transport=transport, digester=digester)
    ctx, _ = _ctx()

    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is True
    assert result.output.startswith("(Digest unavailable")
    assert "# About cats" in result.output  # the raw render is still served
    assert "digest unavailable" in result.summary
    _assert_output_json_safe(result)


# ---------------------------------------------------------------------------
# LLMPageDigester: request shape + bounded wait
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedProvider:
    """LLMProvider stub: fixed reply, records requests, optional delay."""

    reply: str = "the answer"
    delay_seconds: float = 0.0
    requests: list = field(default_factory=list)

    def complete(self, request):  # noqa: ANN001 - protocol shape
        self.requests.append(request)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return LLMResponse(
            stop_reason="end_turn", content=[TextBlock(text=self.reply)]
        )


def test_llm_page_digester_request_shape() -> None:
    provider = _ScriptedProvider(reply="  Cats.  ")
    digester = LLMPageDigester(provider=provider, model="digest-model")
    answer = digester.digest(
        url="https://x", title="t", page_markdown="# page", prompt="what?"
    )
    assert answer == "Cats."
    (request,) = provider.requests
    assert request.model == "digest-model"
    assert request.temperature == 0.0
    assert request.max_tokens is not None
    prompt_text = request.messages[0].content[0].text
    assert "Request: what?" in prompt_text
    assert "# page" in prompt_text
    assert "https://x" in prompt_text


def test_llm_page_digester_times_out() -> None:
    provider = _ScriptedProvider(delay_seconds=0.5)
    digester = LLMPageDigester(
        provider=provider, model="m", timeout_seconds=0.05
    )
    with pytest.raises(TimeoutError):
        digester.digest(url="u", title="t", page_markdown="p", prompt="q")


# ---------------------------------------------------------------------------
# session pack wiring: the "llm" backend binds the digester
# ---------------------------------------------------------------------------


def _pack_ctx(
    ws: Path,
    *,
    backends: dict[str, object],
    plugin_config: Optional[dict[str, dict[str, object]]] = None,
) -> SessionBuildContext:
    ws.mkdir(parents=True, exist_ok=True)
    return SessionBuildContext(
        workspace=WorkspaceRoot.from_path(ws),
        workspace_dir=ws,
        content_store=InMemoryContentStore(),
        exec_env=None,
        model="main-model",
        provider_family=None,
        allowed_tools=frozenset({"WebFetch"}),
        backends=backends,
        capability_flags={},
        plugin_config=plugin_config or {},
    )


def test_web_session_pack_binds_digester_on_main_model(tmp_path: Path) -> None:
    provider = _ScriptedProvider()
    pack = build_web_session_pack(
        _pack_ctx(tmp_path, backends={"llm": provider})
    )
    tool = pack.tools["WebFetch"]
    assert isinstance(tool.digester, LLMPageDigester)
    assert tool.digester.provider is provider
    assert tool.digester.model == "main-model"


def test_web_session_pack_digest_model_override(tmp_path: Path) -> None:
    pack = build_web_session_pack(
        _pack_ctx(
            tmp_path,
            backends={"llm": _ScriptedProvider()},
            plugin_config={"web": {"digest_model": "small-model"}},
        )
    )
    assert pack.tools["WebFetch"].digester.model == "small-model"


def test_web_session_pack_without_llm_backend_keeps_raw_render(
    tmp_path: Path,
) -> None:
    pack = build_web_session_pack(_pack_ctx(tmp_path, backends={}))
    assert pack.tools["WebFetch"].digester is None


# ---------------------------------------------------------------------------
# large page → inline content shrinks, full body stays in the artifact
# ---------------------------------------------------------------------------


def test_webfetch_large_page_truncates_inline_keeps_full_artifact() -> None:
    big = "<html><body>" + ("<p>word here</p>" * 120000) + "</body></html>"
    transport = FakeFetchTransport(pages_by_url={"https://big": big})
    ctx, store = _ctx()
    result = WebFetchTool(transport=transport).invoke(_args("https://big"), ctx)
    assert result.success is True
    # Inline output is bounded with a notice, but nothing is lost — the full
    # markdown survives in the artifact for audit.
    assert "(Content truncated: showing the first" in result.output
    assert len(result.output) < 110_000
    ref = result.artifacts[0]
    assert len(store.get(ref)) > len(result.output.encode("utf-8"))


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


def test_http_fetch_transport_follows_same_host_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        assert request.url.path == "/new"
        return httpx.Response(200, text=_PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text = HttpFetchTransport(client=client).fetch("https://example.com/old")
    assert "About cats" in text


def test_http_fetch_transport_raises_on_cross_host_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": "https://other.example.com/moved"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(CrossHostRedirect) as exc:
        HttpFetchTransport(client=client).fetch("https://example.com/x")
    assert exc.value.location == "https://other.example.com/moved"


def test_http_fetch_transport_bounds_redirect_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/loop"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="too many redirects"):
        HttpFetchTransport(client=client).fetch("https://example.com/loop")


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
    result = tool.invoke(_args("https://example.com/huge"), ctx)
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
    assert set(tools) == {"WebFetch"}
    assert isinstance(tools["WebFetch"].transport, ContainerCurlFetchTransport)


def test_container_fetch_runs_curl_and_renders_markdown() -> None:
    fake = FakeExecEnv(stdout=_PAGE.encode("utf-8"))
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, store = _ctx()

    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is True
    assert fake.calls, "run_argv was not invoked"
    argv = fake.calls[0]
    assert argv[0] == "curl"
    assert argv[-1] == "https://x"
    assert "-A" in argv  # user-agent forwarded
    # Redirects are resolved hop-by-hop in Python (Claude Code's cross-host
    # handshake), so curl itself must NOT follow them...
    assert "-L" not in argv and "-sSL" not in argv
    # ...and status parity with httpx ``raise_for_status`` comes from the
    # ``-w`` metadata on stderr instead of ``--fail``.
    assert "-w" in argv
    # the scripted HTML is rendered by the SAME html_to_markdown as the httpx path
    md = result.output
    assert "# About cats" in md
    assert "[cute](https://example.com/cute)" in md
    assert "- soft" in md
    assert md.startswith("Title: Cats & Kittens\n")
    _assert_output_json_safe(result)


def test_container_fetch_nonzero_exit_degrades() -> None:
    # The container path must degrade exactly like the httpx connection-error
    # path — the transport in use is invisible to the model.
    fake = FakeExecEnv(
        stdout=b"", returncode=6, stderr=b"curl: (6) Could not resolve host"
    )
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://private"), ctx)
    assert result.success is False
    assert "WebFetch failed" in result.summary
    assert "resolve host" in result.summary
    _assert_output_json_safe(result)


def test_container_fetch_http_error_status_degrades() -> None:
    # 403 from a private URL: curl exits 0 (no --fail), the -w metadata names
    # the status, and the tool degrades exactly like the httpx 401/403 path.
    fake = FakeExecEnv(
        stdout=b"<html>denied</html>",
        stderr=b"__noeta_webfetch_meta__ 403 \n",
    )
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://private"), ctx)
    assert result.success is False
    assert "403" in result.summary
    _assert_output_json_safe(result)


def test_container_fetch_timeout_degrades() -> None:
    fake = FakeExecEnv(stdout=b"", returncode=-1, timed_out=True)
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://slow"), ctx)
    assert result.success is False
    assert "WebFetch failed" in result.summary


def test_container_fetch_follows_same_host_redirect() -> None:
    fake = FakeExecEnv(
        script=[
            _outcome(
                stderr=b"__noeta_webfetch_meta__ 302 https://x/next\n"
            ),
            _outcome(stdout=_PAGE.encode("utf-8")),
        ]
    )
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is True
    assert len(fake.calls) == 2
    assert fake.calls[1][-1] == "https://x/next"


def test_container_fetch_cross_host_redirect_surfaces() -> None:
    fake = FakeExecEnv(
        stderr=b"__noeta_webfetch_meta__ 301 https://other.example/moved\n"
    )
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is True
    assert "https://other.example/moved" in result.output
    assert "different host" in result.summary
    assert len(fake.calls) == 1  # not followed


def test_container_fetch_missing_meta_degrades() -> None:
    # An old curl (< 7.63) prints no %{stderr} metadata; that must be a loud,
    # named failure, never a silently mis-read page.
    fake = FakeExecEnv(stdout=_PAGE.encode("utf-8"), stderr=b"")
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://x"), ctx)
    assert result.success is False
    assert "curl" in result.summary


def test_container_fetch_truncated_body_degrades() -> None:
    # Parity with the httpx max_bytes abort: half a page must not render as a
    # "successful" fetch.
    fake = FakeExecEnv(stdout=b"<html>half", stdout_truncated=True)
    tool = build_web_tools(exec_env=fake)["WebFetch"]
    ctx, _ = _ctx()
    result = tool.invoke(_args("https://big"), ctx)
    assert result.success is False
    assert "byte limit" in result.summary
