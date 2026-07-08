"""``web_search``: run a query, render hits to Markdown.

The tool is driven through a fake search transport (no live network); the real
``HttpSearchTransport`` is exercised through ``httpx.MockTransport``. Every
``ToolResult.output`` is checked against ``runtime.tool._encode_output`` for the
B1 invariant (no raw ``ContentRef`` leaked inline). ``build_web_tools`` is keyed
off ``NOETA_WEB_SEARCH_API_KEY`` — present ⇒ the tool appears, absent ⇒ it does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.tool import _encode_output
from noeta.storage.memory import InMemoryContentStore
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES, encoded_len
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.web import (
    ContainerCurlSearchTransport,
    HttpSearchTransport,
    SearchResult,
    WebSearchTool,
    build_web_tools,
)
from noeta.tools.web.search import (
    SEARCH_API_KEY_ENV,
    results_to_markdown,
)


_HITS = [
    SearchResult(
        title="About cats",
        url="https://example.com/cats",
        snippet="Cats are small carnivorous mammals.",
    ),
    SearchResult(
        title="Kitten care",
        url="https://example.com/kittens",
        snippet="Kittens need warmth and frequent feeding.",
    ),
]


@dataclass
class FakeSearchTransport:
    """In-memory query → hits transport; raises for queries in ``raise_for``."""

    hits_by_query: dict[str, list[SearchResult]] = field(default_factory=dict)
    raise_for: frozenset[str] = frozenset()
    error: Exception | None = None
    last_count: int | None = None

    def search(self, query: str, count: int) -> list[SearchResult]:
        self.last_count = count
        if query in self.raise_for:
            raise self.error or RuntimeError(f"transport refused {query}")
        return self.hits_by_query.get(query, [])


@dataclass
class FakeExecEnv:
    """Minimal ``ExecEnv`` stand-in: only ``run_argv`` behaves (sandbox path).

    Records every argv it is handed and returns a scripted ``_RunOutcome`` so a
    container-transport test never shells out.
    """

    stdout: bytes = b""
    returncode: int = 0
    stderr: bytes = b""
    timed_out: bool = False
    calls: list[list[str]] = field(default_factory=list)
    #: (path, body) of every ``write_bytes`` and every ``unlink`` path, so a
    #: test can assert the Tavily key is delivered via a curl --config file and
    #: cleaned up rather than passed in the argv.
    writes: list[tuple[str, bytes]] = field(default_factory=list)
    unlinks: list[str] = field(default_factory=list)

    def run_argv(self, argv, *, cwd, timeout_s, output_cap, runner=None):
        self.calls.append(list(argv))
        return _RunOutcome(
            returncode=self.returncode,
            duration_ms=1,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=self.timed_out,
        )

    def write_bytes(self, path, body) -> None:
        self.writes.append((str(path), bytes(body)))

    def unlink(self, path) -> None:
        self.unlinks.append(str(path))


#: A Tavily response body shared by the container + httpx parse-parity test.
_TAVILY_JSON = {
    "results": [
        {
            "title": "About cats",
            "url": "https://example.com/cats",
            "content": "Cats are mammals.",
        },
        {
            "title": "Kittens",
            "url": "https://example.com/kittens",
            "content": "Baby cats.",
        },
    ]
}


def _ctx() -> tuple[ToolContext, InMemoryContentStore]:
    store = InMemoryContentStore()
    return ToolContext(artifact_store=store), store


def _assert_output_json_safe(result: ToolResult) -> None:
    _encode_output(result.output)


# ---------------------------------------------------------------------------
# tool identity
# ---------------------------------------------------------------------------


def test_web_search_identity_low_risk() -> None:
    tool = WebSearchTool(transport=FakeSearchTransport())
    assert tool.name == "web_search"
    assert tool.risk_level == "low"
    assert tool.description.strip()
    assert tool.input_schema["required"] == ["query"]
    assert tool.input_schema["additionalProperties"] is False


# ---------------------------------------------------------------------------
# happy path: search → markdown → artifact
# ---------------------------------------------------------------------------


def test_web_search_renders_markdown_and_offloads() -> None:
    transport = FakeSearchTransport(hits_by_query={"cats": _HITS})
    ctx, store = _ctx()

    result = WebSearchTool(transport=transport).invoke({"query": "cats"}, ctx)
    assert result.success is True
    assert result.output["query"] == "cats"
    assert result.output["count"] == 2
    md = result.output["content"]
    # numbered list, titles linked, snippets present.
    assert "1. [About cats](https://example.com/cats)" in md
    assert "2. [Kitten care](https://example.com/kittens)" in md
    assert "Cats are small carnivorous mammals." in md
    assert "Kittens need warmth and frequent feeding." in md
    _assert_output_json_safe(result)

    # full markdown is the artifact; content_ref points at it.
    assert len(result.artifacts) == 1
    ref = result.artifacts[0]
    assert result.output["content_ref"]["hash"] == ref.hash
    assert store.get(ref).decode("utf-8") == md
    assert result.output["content_ref"]["media_type"] == "text/markdown"


def test_web_search_deterministic_same_hits_same_artifact() -> None:
    transport = FakeSearchTransport(hits_by_query={"cats": _HITS})
    ctx_a, _ = _ctx()
    ctx_b, _ = _ctx()
    a = WebSearchTool(transport=transport).invoke({"query": "cats"}, ctx_a)
    b = WebSearchTool(transport=transport).invoke({"query": "cats"}, ctx_b)
    assert a.artifacts[0].hash == b.artifacts[0].hash
    assert a.output["content"] == b.output["content"]


def test_web_search_count_clamped_and_forwarded() -> None:
    transport = FakeSearchTransport(hits_by_query={"cats": _HITS})
    ctx, _ = _ctx()
    WebSearchTool(transport=transport).invoke({"query": "cats", "count": 99}, ctx)
    assert transport.last_count == 20  # clamped to _MAX_COUNT
    WebSearchTool(transport=transport).invoke({"query": "cats", "count": 0}, ctx)
    assert transport.last_count == 1  # clamped up to 1
    WebSearchTool(transport=transport).invoke({"query": "cats"}, ctx)
    assert transport.last_count == 5  # default when omitted


# ---------------------------------------------------------------------------
# bad input + transport failures + empty results degrade cleanly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_query", ["", "   ", None, 5])
def test_web_search_rejects_bad_query(bad_query: Any) -> None:
    tool = WebSearchTool(transport=FakeSearchTransport())
    ctx, _ = _ctx()
    result = tool.invoke({"query": bad_query}, ctx)
    assert result.success is False
    _assert_output_json_safe(result)


def test_web_search_degrades_on_transport_failure() -> None:
    transport = FakeSearchTransport(raise_for=frozenset({"boom"}))
    ctx, _ = _ctx()
    result = WebSearchTool(transport=transport).invoke({"query": "boom"}, ctx)
    assert result.success is False
    assert "web_search failed" in result.summary
    _assert_output_json_safe(result)


def test_web_search_auth_failure_names_the_cause() -> None:
    err = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=httpx.Request("POST", "https://api.tavily.com/search"),
        response=httpx.Response(401),
    )
    transport = FakeSearchTransport(raise_for=frozenset({"q"}), error=err)
    ctx, _ = _ctx()
    result = WebSearchTool(transport=transport).invoke({"query": "q"}, ctx)
    assert result.success is False
    assert "401" in result.summary or "Unauthorized" in result.summary


def test_web_search_empty_results_degrade() -> None:
    transport = FakeSearchTransport(hits_by_query={})  # no hits for anything
    ctx, _ = _ctx()
    result = WebSearchTool(transport=transport).invoke({"query": "nothing"}, ctx)
    assert result.success is False
    assert "no results" in result.summary
    assert result.artifacts == []


# ---------------------------------------------------------------------------
# large result set → inline content shrinks, full body stays in the artifact
# ---------------------------------------------------------------------------


def test_web_search_large_result_truncates_inline_keeps_full_artifact() -> None:
    big_hits = [
        SearchResult(
            title=f"Result {i}",
            url=f"https://example.com/{i}",
            snippet="word here " * 2000,
        )
        for i in range(20)
    ]
    transport = FakeSearchTransport(hits_by_query={"big": big_hits})
    ctx, store = _ctx()
    result = WebSearchTool(transport=transport).invoke({"query": "big"}, ctx)
    assert result.success is True
    assert result.output["truncated"] is True
    # inline output respects the canonical byte ceiling ...
    assert encoded_len(result.output) <= INLINE_CONTENT_MAX_BYTES
    # ... but the full markdown survives in the artifact, longer than inline.
    ref = result.artifacts[0]
    assert len(store.get(ref)) > len(result.output["content"].encode("utf-8"))


# ---------------------------------------------------------------------------
# build_web_tools gating on NOETA_WEB_SEARCH_API_KEY
# ---------------------------------------------------------------------------


def test_build_web_tools_omits_web_search_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SEARCH_API_KEY_ENV, raising=False)
    tools = build_web_tools()
    assert set(tools) == {"webfetch"}
    assert "web_search" not in tools


def test_build_web_tools_includes_web_search_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEARCH_API_KEY_ENV, "tvly-test-key")
    tools = build_web_tools()
    assert set(tools) == {"webfetch", "web_search"}
    assert tools["web_search"].risk_level == "low"
    assert isinstance(tools["web_search"], WebSearchTool)


def test_build_web_tools_blank_key_omits_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEARCH_API_KEY_ENV, "   ")  # whitespace-only ⇒ no key
    tools = build_web_tools()
    assert "web_search" not in tools


# ---------------------------------------------------------------------------
# real HttpSearchTransport over httpx.MockTransport (no live network)
# ---------------------------------------------------------------------------


def test_http_search_transport_via_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        assert request.headers["Authorization"] == "Bearer tvly-test"
        # the query body is forwarded
        assert b"cats" in request.content
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "About cats",
                        "url": "https://example.com/cats",
                        "content": "Cats are mammals.",
                    },
                    {
                        "title": "Kittens",
                        "url": "https://example.com/kittens",
                        "content": "Baby cats.",
                    },
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpSearchTransport(api_key="tvly-test", client=client)
    hits = transport.search("cats", 5)
    assert [h.title for h in hits] == ["About cats", "Kittens"]
    assert hits[0].url == "https://example.com/cats"
    assert hits[0].snippet == "Cats are mammals."


def test_http_search_transport_raises_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpSearchTransport(api_key="bad", client=client)
    with pytest.raises(httpx.HTTPStatusError):
        transport.search("q", 5)


# ---------------------------------------------------------------------------
# results_to_markdown helper — deterministic, structure-aware
# ---------------------------------------------------------------------------


def test_results_to_markdown_basic_structure() -> None:
    md = results_to_markdown(_HITS)
    assert md.startswith("1. [About cats](https://example.com/cats)")
    assert "2. [Kitten care](https://example.com/kittens)" in md


def test_results_to_markdown_falls_back_to_url_when_no_title() -> None:
    md = results_to_markdown(
        [SearchResult(title="", url="https://example.com/x", snippet="")]
    )
    assert "1. [https://example.com/x](https://example.com/x)" in md


# ---------------------------------------------------------------------------
# sandbox path: build_web_tools(exec_env=) egresses through the container
# ---------------------------------------------------------------------------


def test_build_web_tools_sandbox_uses_container_search_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEARCH_API_KEY_ENV, "tvly-test-key")
    fake = FakeExecEnv(stdout=json.dumps(_TAVILY_JSON).encode("utf-8"))
    tools = build_web_tools(exec_env=fake)
    assert set(tools) == {"webfetch", "web_search"}
    assert isinstance(tools["web_search"].transport, ContainerCurlSearchTransport)


def test_container_search_runs_curl_post_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEARCH_API_KEY_ENV, "tvly-test-key")
    fake = FakeExecEnv(stdout=json.dumps(_TAVILY_JSON).encode("utf-8"))
    tool = build_web_tools(exec_env=fake)["web_search"]
    ctx, _ = _ctx()

    result = tool.invoke({"query": "cats"}, ctx)
    assert result.success is True
    assert result.output["count"] == 2
    # a curl POST to the Tavily endpoint with the JSON body, HTTP errors failing
    # the run (--fail), and the bearer key delivered OUT-OF-BAND via --config
    argv = fake.calls[0]
    assert argv[0] == "curl"
    assert "POST" in argv
    assert "--fail" in argv
    assert "Content-Type: application/json" in argv
    assert '{"query": "cats", "max_results": 5}' in argv
    assert argv[-1] == "https://api.tavily.com/search"
    # P2b: the key is NEVER in the argv (process table / shell log). It rides in
    # a curl --config file, referenced by -K, and removed after the request.
    assert not any("tvly-test-key" in tok for tok in argv)
    assert "-K" in argv
    config_path = argv[argv.index("-K") + 1]
    assert (config_path, b'header = "Authorization: Bearer tvly-test-key"\n') in fake.writes
    assert config_path in fake.unlinks
    md = result.output["content"]
    assert "1. [About cats](https://example.com/cats)" in md
    assert "2. [Kittens](https://example.com/kittens)" in md
    _assert_output_json_safe(result)


def test_container_and_http_search_parse_identically() -> None:
    # The SAME Tavily JSON through both transports yields identical hits — the
    # two egress paths share _parse_tavily_payload so they cannot drift (R3).
    fake = FakeExecEnv(stdout=json.dumps(_TAVILY_JSON).encode("utf-8"))
    container_hits = ContainerCurlSearchTransport(
        exec_env=fake, api_key="k"
    ).search("cats", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TAVILY_JSON)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    http_hits = HttpSearchTransport(api_key="k", client=client).search("cats", 5)

    assert container_hits == http_hits


def test_container_search_nonzero_exit_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SEARCH_API_KEY_ENV, "tvly-test-key")
    fake = FakeExecEnv(
        stdout=b"", returncode=22, stderr=b"curl: (22) 401 Unauthorized"
    )
    tool = build_web_tools(exec_env=fake)["web_search"]
    ctx, _ = _ctx()
    result = tool.invoke({"query": "q"}, ctx)
    assert result.success is False
    assert "web_search failed" in result.summary
    assert "401" in result.summary
