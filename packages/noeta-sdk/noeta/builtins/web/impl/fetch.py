"""`webfetch` tool — fetch a URL, render it to Markdown, answer `prompt` on it.

Aligned with Claude Code's `WebFetch` surface: `webfetch(url, prompt)` pulls a
page over HTTP(S) through an injected :class:`FetchTransport`, converts the
HTML body to a compact Markdown rendering with a small in-tree heuristic
(headings → ``#``, anchors → ``[text](href)``, list items → ``- ``, paragraphs
preserved; script/style/head stripped, tags otherwise dropped, entities
unescaped, whitespace collapsed), offloads the **full** rendering to a
ContentStore audit artifact, and answers ``prompt`` against the rendering with
an injected :class:`~noeta.builtins.web.impl.digest.PageDigester` — the model
reads the answer, not the raw page. With no digester wired (or on a digest
failure) the tool degrades to returning the rendering itself, inline-capped.

Three more Claude Code alignments live here:

* ``http://`` URLs are upgraded to ``https://`` before fetching.
* A redirect to a **different host** is not followed: the transport raises
  :class:`CrossHostRedirect` and the tool returns the redirect URL to the
  model, which re-issues the fetch explicitly. Same-host redirects are
  followed silently (bounded hops).
* Fetched pages are cached per URL for 15 minutes (successes only — a failure
  cached would turn one transient error into a 15-minute blind spot), so a
  follow-up ``prompt`` about the same page re-digests without re-fetching.

``webfetch`` has ``risk_level="low"`` (a read-only GET; no workspace mutation).

Private / authenticated URLs cannot be reached without credentials: the server
answers 401/403 (or the host is unreachable), the transport raises, and the
tool degrades to ``ToolResult(success=False, ...)`` with a message that names
the cause — it never raises out of the step. This limitation is stated in the
tool's description resource so the model does not try webfetch on intranet /
logged-in pages. A page whose body renders to empty Markdown (blocked, empty,
or script-only) degrades the same way: success=True with zero bytes would read
as "the page had nothing on it" and stop the model from trying another source.

The Markdown conversion is a deliberately minimal, dependency-free heuristic; it
is deterministic given identical input bytes so a resumed run reproduces the same
artifact. The digest answer needs no such property: it rides the recorded
ToolResult, and resume replays the record rather than re-digesting.
"""

from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import httpx

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools.limits import (
    SUMMARY_EMBED_MAX_BYTES,
    truncate_bytes,
)
from noeta.protocols.resources import load_markdown
from noeta.runtime.exec_env import ExecEnv
from noeta.builtins.web.impl.digest import PageDigester
from noeta.builtins.web.impl.search import _outcome_error_text, build_web_search_tool


__all__ = [
    "CrossHostRedirect",
    "FetchTransport",
    "HttpFetchTransport",
    "ContainerCurlFetchTransport",
    "WebFetchTool",
    "build_web_tools",
]


_FETCH_MEDIA_TYPE = "text/markdown"
_MAX_URL_BYTES = 512
_MAX_TITLE_BYTES = 200
#: Inline cap on the rendered page body — both the digest model's reading
#: budget and the raw-render fallback's inline ceiling; the fence exists so one
#: enormous page cannot swamp a context.
_INLINE_PAGE_MAX_CHARS = 100_000
#: Same-host redirect hops followed before giving up.
_MAX_REDIRECT_HOPS = 5
#: Per-URL page-cache TTL — Claude Code's "cached for 15 minutes per URL".
_CACHE_TTL_SECONDS = 900.0
#: Entry cap on the per-tool page cache (each entry is one rendered page, so
#: the cap bounds resident memory, not correctness — an evicted URL simply
#: re-fetches).
_CACHE_MAX_ENTRIES = 16

# Blocks whose *text content* is not body text: scripts, styles, the document
# title (surfaced separately), and the whole <head>.
_STRIP_BLOCKS_RE = re.compile(
    r"<(script|style|title|head|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
# Block-level boundaries that become a blank line (paragraph break) in Markdown.
_BLOCK_BREAK_RE = re.compile(
    r"</?(p|div|section|article|br|tr|table|ul|ol|blockquote|pre)\b[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\f\v]+")
_MANY_BLANKS_RE = re.compile(r"\n{3,}")


def _collapse_inline_ws(text: str) -> str:
    return _INLINE_WS_RE.sub(" ", text).strip()


def _extract_title(raw: str) -> str:
    match = _TITLE_RE.search(raw)
    return _collapse_inline_ws(_html.unescape(match.group(1))) if match else ""


def _strip_tags_text(fragment: str) -> str:
    """Inner text of an HTML fragment: tags dropped, entities unescaped, ws collapsed."""
    return _collapse_inline_ws(_html.unescape(_TAG_RE.sub(" ", fragment)))


def html_to_markdown(raw: str) -> str:
    """Convert an HTML document body to a compact Markdown rendering.

    A small, deterministic heuristic (no readability/lxml dependency): drop the
    non-body blocks, turn structural tags into Markdown markers, then strip any
    remaining tags. Same input bytes → same output (a resumed run reproduces it).
    """
    body = _STRIP_BLOCKS_RE.sub("\n", raw)

    def _heading(m: "re.Match[str]") -> str:
        level = int(m.group(1))
        return f"\n\n{'#' * level} {_strip_tags_text(m.group(2))}\n\n"

    body = _HEADING_RE.sub(_heading, body)

    def _anchor(m: "re.Match[str]") -> str:
        href = _collapse_inline_ws(_html.unescape(m.group(1)))
        text = _strip_tags_text(m.group(2))
        if not text:
            return href
        if not href:
            return text
        return f"[{text}]({href})"

    body = _ANCHOR_RE.sub(_anchor, body)

    def _list_item(m: "re.Match[str]") -> str:
        return f"\n- {_strip_tags_text(m.group(1))}\n"

    body = _LIST_ITEM_RE.sub(_list_item, body)
    # Block boundaries → paragraph break.
    body = _BLOCK_BREAK_RE.sub("\n\n", body)
    # Any tag still standing → dropped.
    body = _TAG_RE.sub(" ", body)
    body = _html.unescape(body)
    # Normalise per-line whitespace, then collapse runs of blank lines.
    lines = [_collapse_inline_ws(line) for line in body.splitlines()]
    out = "\n".join(lines)
    out = _MANY_BLANKS_RE.sub("\n\n", out)
    return out.strip()


def _upgrade_to_https(url: str) -> str:
    """Claude Code parity: an ``http://`` URL is fetched over ``https://``."""
    if url[:7].lower() == "http://":
        return "https://" + url[7:]
    return url


class CrossHostRedirect(RuntimeError):
    """A redirect that leaves the requested host — surfaced, never followed.

    Claude Code parity: the tool hands the redirect URL back to the model,
    which re-issues the fetch explicitly. Following silently would let any
    page teleport the fetch to a host the model never named.
    """

    def __init__(self, url: str, location: str) -> None:
        super().__init__(f"{url} redirects to a different host: {location}")
        self.url = url
        self.location = location


class FetchTransport(Protocol):
    """A url → raw page text seam. Raises on transport / HTTP failure and
    raises :class:`CrossHostRedirect` on a redirect that leaves the host."""

    def fetch(self, url: str) -> str: ...


@dataclass
class WebFetchTool:
    """Fetch a URL and answer ``prompt`` against its Markdown rendering."""

    transport: FetchTransport
    name: str = "WebFetch"
    description: str = field(default=load_markdown(__package__, "webfetch"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "format": "uri",
                    "description": "The URL to fetch content from",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to run on the fetched content",
                },
            },
            "required": ["url", "prompt"],
            "additionalProperties": False,
        }
    )
    #: ``None`` (no provider wired — direct construction, tests) keeps the
    #: raw-render behaviour; the session pack always binds one.
    digester: Optional[PageDigester] = None
    #: Injectable monotonic clock — the cache TTL's time source.
    clock: Callable[[], float] = field(default=time.monotonic)
    #: url → (expires_at, title, rendered markdown). Successes only.
    _page_cache: dict[str, tuple[float, str, str]] = field(
        default_factory=dict, repr=False
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            return ToolResult(
                success=False, summary="WebFetch requires a non-empty 'url'"
            )
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolResult(
                success=False,
                summary=(
                    "WebFetch requires a non-empty 'prompt' — the question to "
                    "answer against the fetched page"
                ),
            )
        url = _upgrade_to_https(url)
        summary_url = truncate_bytes(url, SUMMARY_EMBED_MAX_BYTES)

        cached = self._cache_get(url)
        if cached is not None:
            title, markdown = cached
        else:
            try:
                raw = self.transport.fetch(url)
            except CrossHostRedirect as redirect:
                location = truncate_bytes(redirect.location, _MAX_URL_BYTES)
                return ToolResult(
                    success=True,
                    output=(
                        f"Redirect detected: {summary_url} redirects to a "
                        f"different host and was not followed.\n"
                        f"To fetch it, issue a new WebFetch call with this "
                        f"exact URL: {location}"
                    ),
                    summary=f"redirects to different host: {location}",
                )
            except Exception as exc:  # noqa: BLE001 — degrade, don't crash the step
                return ToolResult(success=False, summary=f"WebFetch failed: {exc}")
            title = _extract_title(raw)
            markdown = html_to_markdown(raw)
            if not markdown.strip():
                # An empty rendering is a failed fetch, not a successful empty
                # page: reporting success=True with 0 bytes reads as "the page
                # had nothing on it", and the model moves on instead of trying
                # another source.
                return ToolResult(
                    success=False,
                    summary=(
                        f"WebFetch got no readable text from {summary_url} — the page "
                        "rendered to empty Markdown (blocked, empty, or script-only); "
                        "try another source"
                    ),
                )
            self._cache_put(url, title, markdown)

        # The full Markdown is the audit artifact regardless of what the model
        # reads inline.
        ref = ctx.artifact_store.put(
            markdown.encode("utf-8"), media_type=_FETCH_MEDIA_TYPE
        )
        page = markdown
        if len(page) > _INLINE_PAGE_MAX_CHARS:
            total = len(page)
            page = page[:_INLINE_PAGE_MAX_CHARS] + (
                f"\n(Content truncated: showing the first "
                f"{_INLINE_PAGE_MAX_CHARS} of {total} characters.)"
            )
        head = (
            f"Title: {truncate_bytes(title, _MAX_TITLE_BYTES)}\n"
            f"URL: {truncate_bytes(url, _MAX_URL_BYTES)}"
        )

        if self.digester is not None:
            try:
                answer = self.digester.digest(
                    url=url, title=title, page_markdown=page, prompt=prompt
                )
            except Exception:  # noqa: BLE001 — a digest failure degrades to the raw render
                answer = ""
            if answer.strip():
                return ToolResult(
                    success=True,
                    output=f"{head}\n\n{answer}",
                    artifacts=[ref],
                    summary=f"fetched {summary_url} ({ref.size}B markdown, digested)",
                )
            return ToolResult(
                success=True,
                output=(
                    "(Digest unavailable — raw page rendering follows.)\n"
                    f"{head}\n\n{page}"
                ),
                artifacts=[ref],
                summary=(
                    f"fetched {summary_url} ({ref.size}B markdown; digest unavailable)"
                ),
            )
        return ToolResult(
            success=True,
            output=f"{head}\n\n{page}",
            artifacts=[ref],
            summary=f"fetched {summary_url} ({ref.size}B markdown)",
        )

    def _cache_get(self, url: str) -> Optional[tuple[str, str]]:
        entry = self._page_cache.get(url)
        if entry is None:
            return None
        expires_at, title, markdown = entry
        if self.clock() >= expires_at:
            del self._page_cache[url]
            return None
        return title, markdown

    def _cache_put(self, url: str, title: str, markdown: str) -> None:
        # FIFO eviction: dicts iterate in insertion order, so the first key is
        # the oldest entry.
        while len(self._page_cache) >= _CACHE_MAX_ENTRIES:
            del self._page_cache[next(iter(self._page_cache))]
        self._page_cache[url] = (self.clock() + _CACHE_TTL_SECONDS, title, markdown)


@dataclass
class HttpFetchTransport:
    """Real HTTP fetch over httpx.

    ``client`` is injectable so tests pass an ``httpx.Client`` backed by an
    ``httpx.MockTransport`` (no live network). ``raise_for_status`` turns a
    private / authenticated URL's 401/403 into a clear ``HTTPStatusError`` that
    the tool surfaces as a failed ``ToolResult``.

    Redirects are followed manually rather than by httpx: a same-host hop is
    transparent (bounded by ``_MAX_REDIRECT_HOPS``), a cross-host hop raises
    :class:`CrossHostRedirect` for the tool to surface.
    """

    timeout: float = 10.0
    user_agent: str = "noeta-webfetch/0.1 (+https://github.com/noeta)"
    client: Optional[httpx.Client] = None
    #: Hard ceiling on the fetched body. ``resp.text`` reads the WHOLE response
    #: into memory, then ``html_to_markdown`` runs several DOTALL regexes over
    #: it — an unbounded / malicious response drives unbounded memory + regex
    #: CPU. Stream and abort past this cap (5 MiB is ample for any real page;
    #: the tool already offloads the rendered body to an artifact + inline cap).
    max_bytes: int = 5 * 1024 * 1024

    def fetch(self, url: str) -> str:
        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            current = url
            for _ in range(_MAX_REDIRECT_HOPS + 1):
                with client.stream(
                    "GET",
                    current,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=False,
                ) as resp:
                    # ``is_redirect`` is only True with a Location header; a
                    # bare 3xx falls through to ``raise_for_status``.
                    if resp.is_redirect:
                        location = resp.headers.get("location", "")
                        target = str(httpx.URL(current).join(location))
                        if httpx.URL(target).host != httpx.URL(current).host:
                            raise CrossHostRedirect(current, target)
                        current = target
                        continue
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ValueError(
                                f"response exceeds {self.max_bytes} byte limit"
                            )
                        chunks.append(chunk)
                    encoding = resp.encoding or "utf-8"
                    return b"".join(chunks).decode(encoding, errors="replace")
            raise RuntimeError(
                f"too many redirects (>{_MAX_REDIRECT_HOPS}) fetching {url}"
            )
        finally:
            if self.client is None:
                client.close()


#: ``curl -w`` format for the container transport: written to STDERR
#: (``%{stderr}``) so the body on stdout stays pure. Requires curl >= 7.63
#: (2018); the sandbox images already carry far newer.
_CURL_META_PREFIX = "__noeta_webfetch_meta__ "
_CURL_META_FORMAT = "%{stderr}" + _CURL_META_PREFIX + "%{http_code} %{redirect_url}\n"


@dataclass
class ContainerCurlFetchTransport:
    """Fetch a URL through the sandbox container via ``curl``.

    In sandbox mode a tool's execution must land inside the session's
    container rather than on the host, so ``webfetch`` egresses by running
    ``curl`` through the ``ExecEnv`` process seam instead of streaming over
    httpx. The fetched HTML is handed to the SAME :func:`html_to_markdown` the
    httpx path uses — only the transport moves into the container.

    Status parity with the httpx path comes from ``curl -w`` metadata on
    stderr (:data:`_CURL_META_FORMAT`): an HTTP >= 400 (a private /
    authenticated URL answering 401/403) raises with the status named — the
    same outcome ``raise_for_status`` produces — and a 3xx is resolved
    hop-by-hop in Python exactly like the httpx loop, so same-host redirects
    follow silently and a cross-host one raises :class:`CrossHostRedirect`.
    """

    exec_env: ExecEnv
    cwd: Path = Path("/")
    timeout: float = 10.0
    user_agent: str = HttpFetchTransport.user_agent
    #: Ceiling on the fetched body, mirroring ``HttpFetchTransport.max_bytes``:
    #: the container ``run_argv`` caps captured output at this size, and a
    #: truncated body raises instead of rendering half a page.
    max_bytes: int = 5 * 1024 * 1024

    def fetch(self, url: str) -> str:
        current = url
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            body, status, redirect_url = self._fetch_hop(current)
            if 300 <= status < 400 and redirect_url:
                if _url_host(redirect_url) != _url_host(current):
                    raise CrossHostRedirect(current, redirect_url)
                current = redirect_url
                continue
            if status >= 400:
                raise RuntimeError(
                    f"webfetch failed: HTTP {status} from {current}"
                )
            return body
        raise RuntimeError(
            f"too many redirects (>{_MAX_REDIRECT_HOPS}) fetching {url}"
        )

    def _fetch_hop(self, url: str) -> tuple[str, int, str]:
        """One redirect-less curl round-trip → (body, status, redirect url)."""
        argv = [
            "curl",
            "-sS",
            "--max-time",
            str(int(self.timeout)),
            "-A",
            self.user_agent,
            "-w",
            _CURL_META_FORMAT,
            url,
        ]
        outcome = self.exec_env.run_argv(
            argv,
            cwd=self.cwd,
            timeout_s=int(self.timeout) + 5,
            output_cap=self.max_bytes,
        )
        if outcome.timed_out:
            raise RuntimeError(
                f"webfetch curl timed out after {self.timeout}s: "
                f"{_outcome_error_text(outcome)}"
            )
        if outcome.returncode != 0:
            raise RuntimeError(
                f"webfetch curl failed (exit {outcome.returncode}): "
                f"{_outcome_error_text(outcome)}"
            )
        if outcome.stdout_truncated:
            raise ValueError(f"response exceeds {self.max_bytes} byte limit")
        meta = self._parse_meta(outcome.stderr.decode("utf-8", errors="replace"))
        if meta is None:
            raise RuntimeError(
                "webfetch curl produced no status metadata — the container's "
                "curl is too old for '%{stderr}' write-out (needs >= 7.63)"
            )
        status, redirect_url = meta
        return outcome.stdout.decode("utf-8", errors="replace"), status, redirect_url

    @staticmethod
    def _parse_meta(stderr_text: str) -> Optional[tuple[int, str]]:
        """The LAST meta line wins — ``-sS`` may interleave warnings before it."""
        for line in reversed(stderr_text.splitlines()):
            if line.startswith(_CURL_META_PREFIX):
                parts = line[len(_CURL_META_PREFIX) :].split(" ", 1)
                try:
                    status = int(parts[0])
                except ValueError:
                    return None
                redirect_url = parts[1].strip() if len(parts) > 1 else ""
                return status, redirect_url
        return None


def _url_host(url: str) -> str:
    return httpx.URL(url).host


def build_web_tools(
    exec_env: Optional[ExecEnv] = None,
    digester: Optional[PageDigester] = None,
) -> dict[str, Tool]:
    """Build the web tool pack (``webfetch`` always; ``web_search`` if keyed).

    The pack is merged into the full built-in pack at the assembly layer
    (``build_session_inputs``) BEFORE the ``allowed_tools`` whitelist filter, so
    only an agent whose spec whitelists ``webfetch`` / ``web_search`` (``main``
    via the full-catalog default) actually receives it — every other preset's
    whitelist omits it (physical isolation).

    ``web_search`` is added only when ``NOETA_WEB_SEARCH_API_KEY`` is set: with no
    key its backend is unreachable, so it is omitted from the pack entirely and
    the model never sees it (the "skip on no connection" shape used for a failed
    MCP server). ``webfetch`` is always present.

    ``digester`` is webfetch's answer path (the session pack binds one off the
    ``"llm"`` backend); ``None`` keeps the raw-render behaviour.

    When ``exec_env`` is supplied (sandbox mode) both tools egress THROUGH the
    container — ``webfetch`` via :class:`ContainerCurlFetchTransport` and
    ``web_search`` via :class:`ContainerCurlSearchTransport` — instead of over
    httpx on the host. ``exec_env is None`` keeps the byte-identical
    host httpx path.
    """
    fetch_transport: FetchTransport = (
        ContainerCurlFetchTransport(exec_env=exec_env)
        if exec_env is not None
        else HttpFetchTransport()
    )
    tools: list[Tool] = [
        WebFetchTool(transport=fetch_transport, digester=digester)
    ]
    search = build_web_search_tool(exec_env=exec_env)
    if search is not None:
        tools.append(search)
    return {t.name: t for t in tools}
