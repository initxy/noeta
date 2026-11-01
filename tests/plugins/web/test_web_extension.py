"""WebFetch and WebSearch: the digest through the runner's model call, the
cache, the HTTPS upgrade, cross-host redirects, the media checks, and the
search result shape."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import Extension
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import ModelRequest, ModelResponse, Text
from noeta.plugins.web import Web
from noeta.plugins.web.fetch import (
    CrossHostRedirect,
    HttpFetchTransport,
    PageCache,
    WebFetchTool,
    preapproved,
    upgrade_to_https,
)
from noeta.plugins.web.render import FetchedPage, html_to_markdown
from noeta.plugins.web.search import HttpSearchTransport, SearchResult, WebSearchTool
from noeta.testing import ScriptedProvider, reply

#: WebFetch is deferred: the scripted model loads it first.
LOAD = reply("", ("ToolSearch", {"query": "select:WebFetch", "max_results": 1}))
PAGE = ("<html><head><title>Hello\n Page</title></head><body><h1>Top</h1>"
        "<p>Price is <b>42</b>.</p><a href='/x'>link</a><script>bad()</script></body></html>")


class FakeFetch:
    def __init__(self, page: str | Exception = PAGE, ctype: str = "text/html") -> None:
        self.page = page
        self.ctype = ctype
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        if isinstance(self.page, Exception):
            raise self.page
        return FetchedPage(self.page, self.ctype)


def _ctx(tmp: Path, task_id: str = "t") -> ToolContext:
    return ToolContext(task_id=task_id, call_id="c", env=LocalExecEnv(str(tmp)), state={},
                       record=lambda *_: None)


def text(out: ToolOutput) -> str:
    return "".join(b.text for b in out.content if isinstance(b, Text))


def test_html_to_markdown() -> None:
    assert html_to_markdown(PAGE) == "# Top\n\nPrice is 42 .\n\n[link](/x)"


async def test_raw_render_without_a_digest(tmp_path: Path) -> None:
    fetch = FakeFetch()
    tool = WebFetchTool(fetch)
    out = await tool.run({"url": "http://example.com/a", "prompt": "price?"}, _ctx(tmp_path))
    assert fetch.urls == ["https://example.com/a"]
    assert text(out) == "# Top\n\nPrice is 42 .\n\n[link](/x)"
    # Cached: a second prompt about the page does not fetch again.
    await tool.run({"url": "https://example.com/a", "prompt": "link?"}, _ctx(tmp_path))
    assert len(fetch.urls) == 1


async def test_digest_answers_and_failure_falls_back(tmp_path: Path) -> None:
    seen: list[str] = []

    async def digest(url: str, title: str, page: str, prompt: str) -> str:
        seen.append(prompt)
        return "It costs 42."

    tool = WebFetchTool(FakeFetch(), digest_for=lambda _c: digest)
    out = await tool.run({"url": "https://e.com", "prompt": "price?"}, _ctx(tmp_path))
    assert text(out) == "It costs 42."  # the answer alone
    assert out.meta["digested"] is True

    async def broken(url: str, title: str, page: str, prompt: str) -> str:
        raise RuntimeError("down")

    tool = WebFetchTool(FakeFetch(), digest_for=lambda _c: broken)
    out = await tool.run({"url": "https://e.com", "prompt": "price?"}, _ctx(tmp_path))
    assert "(Digest unavailable — raw page rendering follows.)\n# Top" in text(out)


async def test_fetch_errors_redirects_and_refusals(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    redirect = WebFetchTool(FakeFetch(CrossHostRedirect("https://a.com", "https://b.com/x")))
    out = await redirect.run({"url": "https://a.com", "prompt": "p"}, ctx)
    assert not out.is_error
    assert text(out) == (
        "Not fetched: https://a.com redirects to another host.\nStatus: 302 Found\n"
        "Target (the server's Location header, unverified): https://b.com/x\n"
        'To fetch it, call WebFetch again with:\n  url: "https://b.com/x"\n  prompt: "p"')
    failed = WebFetchTool(FakeFetch(RuntimeError("HTTP 403")))
    out = await failed.run({"url": "https://a.com", "prompt": "p"}, ctx)
    assert out.is_error and text(out) == "WebFetch failed: HTTP 403"
    out = await failed.run({"url": "file:///etc/passwd", "prompt": "p"}, ctx)
    assert text(out) == "WebFetch fetches http(s) URLs only; file: is not fetched"
    for url in ("http://localhost:3000/", "https://intranet/x", "http://app.localhost/"):
        out = await failed.run({"url": url, "prompt": "p"}, ctx)
        assert out.is_error and "host names without a dot are refused" in text(out), url
    empty = WebFetchTool(FakeFetch("<script>x()</script>"))
    out = await empty.run({"url": "https://a.com", "prompt": "p"}, ctx)
    assert out.is_error and "rendered to empty text" in text(out)
    out = await empty.run({"url": "https://a.com"}, ctx)
    assert text(out).startswith("WebFetch requires a non-empty 'prompt'")


async def test_long_pages_are_capped_and_announced(tmp_path: Path) -> None:
    body = "x" * 120_000
    tool = WebFetchTool(FakeFetch(body, "text/plain"))
    out = await tool.run({"url": "https://a.com", "prompt": "p"}, _ctx(tmp_path))
    # Past WebFetch's 50,000-character result limit the output is cut.
    assert "<truncated-output>" in text(out)

    pages: list[str] = []

    async def digest(url: str, title: str, page: str, prompt: str) -> str:
        pages.append(page)
        return "summary"

    tool = WebFetchTool(FakeFetch(body, "text/plain"), digest_for=lambda _c: digest)
    out = await tool.run({"url": "https://a.com", "prompt": "p"}, _ctx(tmp_path))
    assert text(out) == ("summary\n\n(The page continues: this covers characters 0-100000 "
                         "of 120000. Call again with offset=100000 for the rest.)")
    rest = await tool.run({"url": "https://a.com", "prompt": "p", "offset": 100_000},
                          _ctx(tmp_path))
    assert text(rest) == "summary" and len(pages[1]) == 20_000
    past = await tool.run({"url": "https://a.com", "prompt": "p", "offset": 200_000},
                          _ctx(tmp_path))
    assert not past.is_error and text(past).startswith("Nothing is left to read")


def test_https_upgrade_spares_loopback() -> None:
    assert upgrade_to_https("http://example.com/x") == "https://example.com/x"
    assert upgrade_to_https("http://localhost:3000/") == "http://localhost:3000/"
    assert upgrade_to_https("http://127.0.0.1/") == "http://127.0.0.1/"


def test_page_cache_expires_and_evicts() -> None:
    now = [0.0]
    cache = PageCache(clock=lambda: now[0], max_entries=2)
    cache.put("a", "t", "m")
    cache.put("b", "t", "m")
    cache.put("c", "t", "m")
    assert cache.entry("a") is None and cache.entry("c") == ("t", "m", False, "")
    now[0] = 901
    assert cache.entry("c") is None


async def test_http_transport_redirects_and_media() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        if request.url.path == "/away":
            return httpx.Response(302, headers={"location": "https://other.org/"})
        if request.url.path == "/login":
            return httpx.Response(302, headers={"location": "https://me:pw@site.com/in"})
        if request.url.path == "/img":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=b"<p>hi</p>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = HttpFetchTransport(client=client)
    page = await transport.fetch("https://site.com/old")
    assert str(page) == "<p>hi</p>" and page.content_type.startswith("text/html")
    with pytest.raises(CrossHostRedirect):
        await transport.fetch("https://site.com/away")
    with pytest.raises(CrossHostRedirect):  # credentials in the target
        await transport.fetch("https://site.com/login")
    image = await transport.fetch("https://site.com/img")
    assert image.raw == b"x" and image.content_type == "image/png"
    await client.aclose()


def test_preapproved_sites_follow_claude_codes_rule() -> None:
    for url in ("https://docs.python.org/3/library/os.html", "https://go.dev/doc",
                "https://go.dev/doc/effective_go", "https://github.com/anthropics/x/issues/1",
                "https://github.com/anthropic-experimental/x/blob/main/a.md",
                "https://raw.githubusercontent.com/anthropics/x/refs/heads/main/a.md"):
        assert preapproved(url), url
    for url in ("https://go.dev/blog", "https://go.dev/docs", "https://go.dev/doc/../blog",
                "https://go.dev/doc%2F..%2Fblog", "https://github.com/anthropic-experimental/x/pulls",
                "https://raw.githubusercontent.com/anthropics/x/dev/a.md",
                "https://docs.python.org.example.com/", "https://example.com/docs.python.org"):
        assert not preapproved(url), url


async def test_a_documentation_sites_markdown_is_the_result(tmp_path: Path) -> None:
    digested: list[str] = []

    async def digest(url: str, title: str, page: str, prompt: str) -> str:
        digested.append(url)
        return "summary"

    def tool(page: str, ctype: str) -> tuple[WebFetchTool, FakeFetch]:
        fetch = FakeFetch(page, ctype)
        return WebFetchTool(fetch, digest_for=lambda _c: digest), fetch

    md, fetch = tool("# Title\n\nBody.", "text/markdown; charset=utf-8")
    out = await md.run({"url": "https://docs.python.org/3/x.md", "prompt": "p"}, _ctx(tmp_path))
    assert text(out) == "# Title\n\nBody." and out.meta["markdown"] is True and not digested
    # From the cache too, and from an offset.
    out = await md.run({"url": "https://docs.python.org/3/x.md", "prompt": "q", "offset": 9},
                       _ctx(tmp_path))
    assert text(out) == "Body." and len(fetch.urls) == 1 and not digested
    # Another site, HTML, or a page the window cannot hold go to the digest.
    other, _ = tool("# Title", "text/markdown")
    out = await other.run({"url": "https://example.com/x.md", "prompt": "p"}, _ctx(tmp_path))
    html, _ = tool(PAGE, "text/html")
    await html.run({"url": "https://docs.python.org/3/", "prompt": "p"}, _ctx(tmp_path))
    long, _ = tool("x" * 100_000, "text/markdown")
    await long.run({"url": "https://docs.python.org/3/", "prompt": "p"}, _ctx(tmp_path))
    assert text(out) == "summary" and len(digested) == 3


async def test_the_http_transport_asks_for_markdown_first() -> None:
    accepts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        accepts.append(request.headers["accept"])
        return httpx.Response(200, headers={"content-type": "text/markdown"}, content=b"# a")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    page = await HttpFetchTransport(client=client).fetch("https://docs.python.org/3/")
    assert accepts == ["text/markdown, text/html, */*"] and page.content_type == "text/markdown"
    await client.aclose()


def _mock_fetch(handler: Any) -> WebFetchTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebFetchTool(HttpFetchTransport(client=client))


async def test_an_http_error_status_is_a_report_not_a_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/busy":
            return httpx.Response(503, headers={"retry-after": "30"}, content=b"down")
        return httpx.Response(404, content=b"<p>missing</p>")

    tool = _mock_fetch(handler)
    out = await tool.run({"url": "https://site.com/gone", "prompt": "p"}, _ctx(tmp_path))
    assert not out.is_error
    assert text(out).startswith("The server answered HTTP 404 Not Found.\n")
    assert "gh" in text(out) and "Retry-After" not in text(out)
    busy = await tool.run({"url": "https://site.com/busy", "prompt": "p"}, _ctx(tmp_path))
    assert not busy.is_error
    assert "HTTP 503 Service Unavailable" in text(busy) and "Retry-After: 30" in text(busy)


async def test_a_redirect_report_names_its_status_and_the_call_to_make(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/a"})
        if request.url.host == "www.example.com":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")
        return httpx.Response(302, headers={"location": "https://other.org/x"})

    tool = _mock_fetch(handler)
    same = await tool.run({"url": "https://example.com/a", "prompt": "p"}, _ctx(tmp_path))
    assert text(same) == "ok"  # adding or dropping www. is the same site
    out = await tool.run({"url": "https://a.com/start", "prompt": "the price", "offset": 7},
                         _ctx(tmp_path))
    assert not out.is_error
    body = text(out)
    assert "https://a.com/start" in body and "https://other.org/x" in body
    assert "302 Found" in body
    assert 'url: "https://other.org/x"' in body and 'prompt: "the price"' in body
    assert "offset: 7" in body


async def test_redirects_are_followed_ten_times_on_one_site() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        if n < 10:
            return httpx.Response(302, headers={"location": f"/{n + 1}"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"end")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert str(await HttpFetchTransport(client=client).fetch("https://s.com/0")) == "end"
    with pytest.raises(RuntimeError, match="too many redirects"):
        await HttpFetchTransport(client=client).fetch("https://s.com/-1")


async def test_long_or_credentialed_urls_are_invalid(tmp_path: Path) -> None:
    transport = HttpFetchTransport()
    assert transport.timeout == 60.0 and transport.max_bytes == 10 * 1024 * 1024
    fetch = FakeFetch()
    tool = WebFetchTool(fetch)
    for url in ("https://e.com/" + "a" * 2000, "https://user:pw@e.com/"):
        out = await tool.run({"url": url, "prompt": "p"}, _ctx(tmp_path))
        assert out.is_error and text(out) == "Invalid URL", url
    assert fetch.urls == []


async def test_an_offset_past_the_end_is_said_not_failed(tmp_path: Path) -> None:
    tool = WebFetchTool(FakeFetch("x" * 50, "text/plain"))
    out = await tool.run({"url": "https://a.com", "prompt": "p", "offset": 60},
                         _ctx(tmp_path))
    assert not out.is_error
    assert text(out) == "Nothing is left to read from offset 60: the page's text is 50 characters long."


async def test_binary_content_is_saved_for_the_model_to_read(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"%PDF-1.4 hello")

    digested: list[str] = []

    async def digest(url: str, title: str, page: str, prompt: str) -> str:
        digested.append(page)
        return "answer"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = WebFetchTool(HttpFetchTransport(client=client), digest_for=lambda _c: digest)
    ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)), state={},
                      record=lambda *_: None, spill_dir=str(tmp_path / "spill"))
    out = await tool.run({"url": "https://s.com/doc.pdf", "prompt": "p"}, ctx)
    assert not out.is_error
    # Only the note: the bytes are neither rendered nor summarized.
    note = text(out)
    assert note.startswith("[The response is binary (application/pdf, 14 bytes); it was saved to ")
    saved = note.removeprefix(
        "[The response is binary (application/pdf, 14 bytes); it was saved to ").rstrip("]")
    assert Path(saved).read_bytes() == b"%PDF-1.4 hello" and saved.endswith(".pdf")
    assert digested == []
    # A second fetch within the cache's life still names the saved file.
    again = await tool.run({"url": "https://s.com/doc.pdf", "prompt": "other"}, ctx)
    assert not again.is_error and text(again) == note and digested == []
    without = await tool.run({"url": "https://s.com/other.pdf", "prompt": "p"}, _ctx(tmp_path))
    assert without.is_error and "application/pdf" in text(without)


async def test_a_raw_body_with_nowhere_to_save_it_is_an_error(tmp_path: Path) -> None:
    # A transport may hand back raw bytes of a type the tool reads as text.
    class RawFetch:
        async def fetch(self, url: str) -> FetchedPage:
            return FetchedPage("", "text/plain", raw=b"hello")

    tool = WebFetchTool(RawFetch())
    out = await tool.run({"url": "https://s.com/a.txt", "prompt": "p"}, _ctx(tmp_path))
    assert out.is_error and "None" not in text(out)
    assert text(out) == ("WebFetch failed: the response is binary (text/plain, 5 bytes) and "
                         "there is nowhere to save it")
    again = await tool.run({"url": "https://s.com/a.txt", "prompt": "p"}, _ctx(tmp_path))
    assert again.is_error  # not cached as a note


class FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, count: int) -> list[SearchResult]:
        self.calls.append((query, count))
        return self.results


async def test_search_results_and_counts(tmp_path: Path) -> None:
    fake = FakeSearch([SearchResult("T", "https://u", "snip"), SearchResult("", "https://v", "")])
    tool = WebSearchTool(fake)
    out = await tool.run({"query": "q", "mode": "extended"}, _ctx(tmp_path))
    assert text(out) == (
        'Web search results for query: "q"\n\n'
        'Links: [{"title": "T", "url": "https://u"}, {"title": "https://v", "url": "https://v"}]'
        "\n\n1. [T](https://u)\n   snip\n\n2. [https://v](https://v)\n\n"
        "Reminder: cite the sources above in your reply, as Markdown links.")
    await tool.run({"query": "q", "mode": "standard"}, _ctx(tmp_path))
    assert [c for _, c in fake.calls] == [10, 5]
    assert tool.input_schema["required"] == ["query", "mode"]
    assert "count" not in tool.input_schema["properties"]
    none = await WebSearchTool(FakeSearch([])).run({"query": "zz"}, _ctx(tmp_path))
    assert not none.is_error
    assert text(none) == 'Web search results for query: "zz"\n\nNo links found.'
    assert text(await tool.run({}, _ctx(tmp_path))) == "WebSearch requires a non-empty 'query'"
    both = await tool.run({"query": "q", "allowed_domains": ["a.com"],
                           "blocked_domains": ["b.com"]}, _ctx(tmp_path))
    assert both.is_error and text(both) == (
        "WebSearch takes allowed_domains or blocked_domains, not both in one search")


async def test_tavily_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"results": [
            {"title": "A\nB", "url": "https://a", "content": "c"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hits = await HttpSearchTransport("k", client=client).search("q", 3)
    assert hits == [SearchResult("A B", "https://a", "c")]
    await client.aclose()


async def test_domain_filters_and_mode_reach_tavily_and_the_hits(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [
            {"title": "a", "url": "https://docs.a.com/x", "content": ""},
            {"title": "b", "url": "https://b.com/y", "content": ""},
            {"title": "c", "url": "https://evil.a.com/z", "content": ""}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = WebSearchTool(HttpSearchTransport("k", client=client))
    out = await tool.run({"query": "q", "mode": "extended", "allowed_domains": ["a.com"]},
                         _ctx(tmp_path))
    assert sent[0]["include_domains"] == ["a.com"] and sent[0]["search_depth"] == "advanced"
    assert "https://docs.a.com/x" in text(out) and "https://b.com/y" not in text(out)
    out = await tool.run({"query": "q", "mode": "extended", "blocked_domains": ["evil.a.com"]},
                         _ctx(tmp_path))
    assert sent[1]["exclude_domains"] == ["evil.a.com"]
    assert "https://b.com/y" in text(out) and "evil" not in text(out)
    # A transport without the keywords still gets its hits filtered.
    plain = WebSearchTool(FakeSearch([SearchResult("x", "https://b.com", "")]))
    out = await plain.run({"query": "q", "mode": "standard", "blocked_domains": ["b.com"]},
                          _ctx(tmp_path))
    assert "No links found." in text(out)
    await client.aclose()


def test_search_is_offered_only_with_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOETA_WEB_SEARCH_API_KEY", raising=False)
    names = [t.name for t in Web()._tools]
    assert names == ["WebFetch"]
    assert [t.name for t in Web(search_api_key="k")._tools] == ["WebFetch", "WebSearch"]
    monkeypatch.setenv("NOETA_WEB_SEARCH_API_KEY", "k")
    assert [t.name for t in Web()._tools] == ["WebFetch", "WebSearch"]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_webfetch_model_names_the_digest_model(store: SqliteStore,
                                                     tmp_path: Path) -> None:
    def digest_reply(request: ModelRequest) -> ModelResponse:
        assert request.model == "cheap"
        return reply("ok")

    provider = ScriptedProvider([LOAD, reply("", ("WebFetch", {"url": "https://e.com",
                                                         "prompt": "p"})),
                                 digest_reply, reply("done")])
    agent = Agent(name="a", model="m",
                  extensions=[Web(fetch_transport=FakeFetch(), webfetch_model="cheap")])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    client = Client(store)
    await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    assert len(provider.requests) == 4


async def test_another_digest_model_gets_none_of_the_main_headers(
        store: SqliteStore, tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def digest_reply(request: ModelRequest) -> ModelResponse:
        seen.append(dict(request.headers))
        return reply("ok")

    provider = ScriptedProvider([LOAD, reply("", ("WebFetch", {"url": "https://e.com",
                                                         "prompt": "p"})),
                                 digest_reply, reply("done")])

    class Headers(Extension):
        name = "headers"

        async def before_model(self, view: Any, request: ModelRequest) -> ModelRequest:
            import msgspec

            return msgspec.structs.replace(request, headers={"authorization": "Bearer gw"})

    agent = Agent(name="a", model="m", extensions=[
        Headers(), Web(fetch_transport=FakeFetch(), webfetch_model="other-vendor")])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    await Client(store).create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    assert seen == [{}]


async def test_a_digest_needs_no_earlier_hook_in_this_process(tmp_path: Path) -> None:
    # As on a worker that picks the task up just to run this call (after an
    # approval, or again after a crash): nothing else ran here before.
    seen: list[ModelRequest] = []

    async def complete(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return reply("It costs 42.")

    web = Web(fetch_transport=FakeFetch())
    ctx = ToolContext(task_id="t", call_id="c", env=LocalExecEnv(str(tmp_path)), state={},
                      record=lambda *_: None, model="main-model", complete=complete)
    out = await web.fetch.run({"url": "https://e.com", "prompt": "price?"}, ctx)
    assert text(out) == "It costs 42." and [r.model for r in seen] == ["main-model"]


def test_per_task_bindings_are_bounded() -> None:
    from noeta.core.tool import ToolContext
    from noeta.plugins.web import TASKS_KEPT

    web = Web(fetch_transport=FakeFetch())
    for i in range(TASKS_KEPT + 10):
        ctx = ToolContext(task_id=f"t{i}", call_id="c", env=None,  # type: ignore[arg-type]
                          state={}, record=lambda *_: None)
        web._cache_for(ctx)
    assert len(web._caches) == TASKS_KEPT and "t0" not in web._caches


def test_the_user_agent_points_at_the_project() -> None:
    assert HttpFetchTransport().user_agent.endswith("(+https://github.com/initxy/noeta)")


async def test_digest_runs_through_the_runner(store: SqliteStore, tmp_path: Path) -> None:
    def digest_reply(request: ModelRequest) -> ModelResponse:
        assert request.model == "main-model" and request.max_tokens == 4096
        assert not request.thinking and not request.tools
        # Like every call made beside the step (a title, a summary), the
        # digest carries none of the headers a hook put on the main request.
        assert request.headers == {}
        prompt = request.messages[0].text
        assert prompt.startswith("You are answering a request about a web page")
        assert "\nRequest: price?\n" in prompt and "Price is 42" in prompt
        return reply("The price is 42.")

    provider = ScriptedProvider([LOAD, reply("", ("WebFetch", {"url": "https://e.com",
                                                         "prompt": "price?"})),
                                 digest_reply, reply("done")])

    class Headers(Extension):
        name = "headers"

        async def before_model(self, view: Any, request: ModelRequest) -> ModelRequest:
            import msgspec

            return msgspec.structs.replace(request, headers={"x-request": "r1"})

    agent = Agent(name="a", model="main-model",
                  extensions=[Headers(), Web(fetch_transport=FakeFetch())])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [_loaded, result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    assert result.text == "The price is 42."
