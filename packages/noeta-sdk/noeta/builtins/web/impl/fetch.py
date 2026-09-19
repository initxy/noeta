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

* ``http://`` URLs are upgraded to ``https://`` before fetching, except on
  loopback hosts — nothing falls back from a failed upgrade, and a local dev
  server on plain HTTP would otherwise be unreachable.
* A redirect to a **different host** is not followed: the transport raises
  :class:`CrossHostRedirect` and the tool states where the URL points, leaving
  the decision to fetch it with the model. Same-host redirects are followed
  silently (bounded hops).
* Fetched pages are cached per URL for 15 minutes (successes only — a failure
  cached would turn one transient error into a 15-minute blind spot), so a
  follow-up ``prompt`` about the same page re-digests without re-fetching.

``webfetch`` has ``risk_level="low"`` (a read-only GET; no workspace mutation),
so whether a fetch needs a human is decided per call rather than by risk grade:
``SdkHost`` builds an approval predicate off
``HostConfig.webfetch_allowed_hosts`` (:mod:`noeta.client.webfetch_policy`) —
the same shape as ``Bash``'s allowlist gate, so a listed host stays prompt-free
and the tool keeps its ``low`` grade. The tool itself fences nothing by
address: an agent holding ``Bash`` reaches the same target with one ``curl``,
so a per-address refusal here would protect nothing. A host that needs an
egress boundary enforces it at the network or the sandbox.

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
import ipaddress
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import httpx

from noeta.client.webfetch_policy import unsupported_scheme_refusal
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools.limits import (
    SUMMARY_EMBED_MAX_BYTES,
    truncate_bytes,
)
from noeta.protocols.resources import load_markdown
from noeta.runtime.exec_env import ExecEnv
from noeta.builtins.web.impl.digest import PageDigester
from noeta.builtins.web.impl.search import (
    WEB_SOURCE_LINE,
    _outcome_error_text,
    build_web_search_tool,
    collapse_whitespace,
)


__all__ = [
    "CrossHostRedirect",
    "FetchTransport",
    "HttpFetchTransport",
    "ContainerCurlFetchTransport",
    "PAGE_CACHE_SLOT",
    "PageCache",
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
#: Entry cap on the page cache (each entry is one rendered page, so the cap
#: bounds resident memory, not correctness — an evicted URL simply
#: re-fetches).
_CACHE_MAX_ENTRIES = 16
#: The first line of every result that carries server-supplied text, ahead of
#: ``Title:`` / ``URL:``. Shared with ``WebSearch`` so both tools say the same
#: thing about the same kind of content.
_SOURCE_LINE = WEB_SOURCE_LINE


class PageCache:
    """The 15-minute per-URL page cache (successes only), one per task.

    The tool — like the whole Engine — is built afresh every turn, so a
    cache on the tool instance would forget the page at every turn boundary
    and Claude Code's "cached for 15 minutes" would hold within a turn only.
    The session pack therefore keeps the cache in the task's local slot
    :data:`PAGE_CACHE_SLOT` (``plugin_config["web"]["task_slot"]``, bound by
    the host) and hands it to each turn's tool. Per task, never wider: a
    page fetched through one task's egress (its sandbox container, its
    tenant's network) must never answer another task's fetch. Tests inject
    their own instance with a fake clock. Thread-safe.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
        max_entries: int = _CACHE_MAX_ENTRIES,
    ) -> None:
        self.clock = clock
        self._ttl = ttl_seconds
        self._max = max_entries
        #: url → (expires_at, title, rendered markdown).
        self._pages: dict[str, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    def get(self, url: str) -> Optional[tuple[str, str]]:
        with self._lock:
            entry = self._pages.get(url)
            if entry is None:
                return None
            expires_at, title, markdown = entry
            if self.clock() >= expires_at:
                del self._pages[url]
                return None
            return title, markdown

    def put(self, url: str, title: str, markdown: str) -> None:
        with self._lock:
            # FIFO eviction: dicts iterate in insertion order, so the first
            # key is the oldest entry.
            while len(self._pages) >= self._max:
                del self._pages[next(iter(self._pages))]
            self._pages[url] = (self.clock() + self._ttl, title, markdown)

    def clear(self) -> None:
        with self._lock:
            self._pages.clear()


#: The task-local slot the session pack keeps a task's :class:`PageCache` in.
PAGE_CACHE_SLOT = "web.page_cache"

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
    """The document title, flattened to ONE line.

    ``_collapse_inline_ws`` deliberately keeps newlines (``html_to_markdown``
    needs the line structure), but the title lands inside two line-oriented
    templates — the ``Source:`` / ``Title:`` / ``URL:`` header of the result
    and the digest prompt's ``Request:`` block — where a newline would let a
    page write a header line of its own. So the title collapses everything."""
    match = _TITLE_RE.search(raw)
    return collapse_whitespace(_html.unescape(match.group(1))) if match else ""


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


def _url_host(url: str) -> str:
    return httpx.URL(url).host


def _is_loopback_url(url: str) -> bool:
    """Whether ``url`` points at this machine (``localhost``, ``127.0.0.0/8``,
    ``::1``). A URL too malformed to parse is not loopback — it fails at fetch
    time, where the error can name the cause."""
    try:
        host = _url_host(url)
    except Exception:  # noqa: BLE001 — a malformed URL fails at fetch time
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _upgrade_to_https(url: str) -> str:
    """Claude Code parity: an ``http://`` URL is fetched over ``https://``.

    Loopback is the exception, and it has to be: there is no fallback from a
    failed upgrade, so a dev server on ``http://localhost:3000`` would be
    unreachable for the whole session. A loopback request never leaves the
    machine, which is the only thing the upgrade protects.
    """
    if url[:7].lower() != "http://":
        return url
    if _is_loopback_url(url):
        return url
    return "https://" + url[7:]


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
    #: The per-URL page cache (successes only). The session pack passes the
    #: task's own (see :class:`PageCache`); a bare tool keeps a private one,
    #: and a test passes ``PageCache(clock=...)``.
    cache: PageCache = field(default_factory=PageCache)

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

        # ``file:`` / ``gopher:`` and friends are not something this tool
        # fetches. httpx would refuse them itself, but the container transport
        # hands the URL to curl, which would read a container file and call it
        # a page — so the scheme is settled here, once, for both transports.
        refusal = unsupported_scheme_refusal(url)
        if refusal is not None:
            return ToolResult(success=False, summary=refusal)

        cached = self._cache_get(url)
        if cached is not None:
            title, markdown = cached
        else:
            try:
                raw = self.transport.fetch(url)
            except CrossHostRedirect as redirect:
                # The location comes from the server, so it gets the same
                # provenance line a fetched page gets, and it is stated as a
                # fact: which URL to fetch next stays the model's call, not a
                # command a remote host got to write into the result.
                location = collapse_whitespace(
                    truncate_bytes(redirect.location, _MAX_URL_BYTES)
                )
                return ToolResult(
                    success=True,
                    output=(
                        f"{_SOURCE_LINE}\n"
                        f"{truncate_bytes(url, _MAX_URL_BYTES)} redirects to a "
                        f"different host: {location}\n"
                        "The redirect was not followed and nothing was "
                        "fetched. Fetch that URL if it is what you need."
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
        # The note inside ``page`` reaches whoever reads the page: the digest
        # model, or the calling model on the raw-render paths. On the digest
        # path the caller reads only the answer, so the same fact is restated
        # on the result it does read — otherwise a partial answer looks whole.
        digest_coverage_note = ""
        if len(page) > _INLINE_PAGE_MAX_CHARS:
            total = len(page)
            page = page[:_INLINE_PAGE_MAX_CHARS] + (
                f"\n(Content truncated: showing the first "
                f"{_INLINE_PAGE_MAX_CHARS} of {total} characters.)"
            )
            digest_coverage_note = (
                f"\n\n(The answer covers only the first "
                f"{_INLINE_PAGE_MAX_CHARS} of {total} characters of the page.)"
            )
        head = (
            f"{_SOURCE_LINE}\n"
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
                    output=f"{head}\n\n{answer}{digest_coverage_note}",
                    artifacts=[ref],
                    summary=f"fetched {summary_url} ({ref.size}B markdown, digested)",
                )
            return ToolResult(
                success=True,
                output=(
                    f"{head}\n\n"
                    "(Digest unavailable — raw page rendering follows.)\n"
                    f"{page}"
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
        return self.cache.get(url)

    def _cache_put(self, url: str, title: str, markdown: str) -> None:
        self.cache.put(url, title, markdown)


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


#: Stem of the ``curl -w`` status marker; the full prefix carries a per-call
#: nonce (:func:`_curl_meta_prefix`).
_CURL_META_STEM = "__noeta_webfetch_meta_"


def _curl_meta_prefix() -> str:
    """A fresh, unguessable marker prefix for one ``curl -w`` round-trip.

    The status line is control data that decides whether the fetch raises and
    where a redirect goes, and it can arrive in the same stream as the page:
    the shipped sandbox ExecEnv merges stdout and stderr into one (see
    :class:`~noeta.builtins.sandbox.impl.exec_env.AioSandboxExecEnv`). A page
    that spelled a fixed marker itself could forge that line, so the marker
    carries a nonce nothing on the wire can guess.
    """
    return f"{_CURL_META_STEM}{secrets.token_hex(8)}__ "


def _curl_meta_format(prefix: str) -> str:
    """``curl -w`` format for the container transport: written to STDERR
    (``%{stderr}``) so a split stream keeps the body on stdout pure; a merged
    stream simply carries the line along with the body. Requires curl >= 7.63
    (2018); the sandbox images already carry far newer."""
    return "%{stderr}" + prefix + "%{http_code} %{redirect_url}\n"


def _split_curl_meta(
    stream: bytes, marker: bytes
) -> tuple[bytes, Optional[tuple[int, str]]]:
    """Cut the ``curl -w`` status line out of ``stream``.

    Returns ``(stream without that line, (status, redirect url))``, or the
    stream untouched and ``None`` when it carries no parsable line. Every byte
    that is not the line itself survives, in order, because that stream may be
    the page body.

    Two things the obvious parse gets wrong on a merged stream. The marker is
    not required to START a line: a body that does not end in a newline butts
    straight against it. And it is not necessarily LAST: ``curl -sS`` may print
    a warning after it, and curl's own stdout buffering can flush the tail of
    the body after the write-out has already gone to the unbuffered stderr. So
    the search is for the last occurrence anywhere, and the splice keeps what
    follows the line as well as what precedes it.
    """
    index = stream.rfind(marker)
    if index < 0:
        return stream, None
    end = stream.find(b"\n", index)
    fields = stream[index + len(marker) : end if end >= 0 else len(stream)]
    parsed = _parse_curl_meta_fields(fields.decode("utf-8", errors="replace"))
    if parsed is None:
        return stream, None
    rest = stream[:index] + (stream[end + 1 :] if end >= 0 else b"")
    return rest, parsed


def _parse_curl_meta_fields(fields: str) -> Optional[tuple[int, str]]:
    """``"<http_code> <redirect_url>"`` → ``(status, redirect url)``.

    ``None`` when the status is not an integer — the line is then left in the
    stream and the caller reports missing metadata rather than acting on a
    status it had to guess.
    """
    parts = fields.split(" ", 1)
    try:
        status = int(parts[0])
    except ValueError:
        return None
    return status, parts[1].strip() if len(parts) > 1 else ""


@dataclass
class ContainerCurlFetchTransport:
    """Fetch a URL through the sandbox container via ``curl``.

    In sandbox mode a tool's execution must land inside the session's
    container rather than on the host, so ``webfetch`` egresses by running
    ``curl`` through the ``ExecEnv`` process seam instead of streaming over
    httpx. The fetched HTML is handed to the SAME :func:`html_to_markdown` the
    httpx path uses — only the transport moves into the container.

    Status parity with the httpx path comes from ``curl -w`` metadata
    (:func:`_curl_meta_format`): an HTTP >= 400 (a private / authenticated URL
    answering 401/403) raises with the status named — the same outcome
    ``raise_for_status`` produces — and a 3xx is resolved hop-by-hop in Python
    exactly like the httpx loop, so same-host redirects follow silently and a
    cross-host one raises :class:`CrossHostRedirect`.

    The write-out goes to stderr, but the transport reads it from **either**
    stream: the shipped sandbox ExecEnv merges stdout and stderr into one and
    always reports ``stderr=b""``, so on that backend the status line arrives
    inside the body and is cut back out of it (:func:`_split_curl_meta`) —
    byte for byte, because those bytes are the page.
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
        prefix = _curl_meta_prefix()
        argv = [
            "curl",
            "-sS",
            "--max-time",
            str(int(self.timeout)),
            "-A",
            self.user_agent,
            "-w",
            _curl_meta_format(prefix),
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
            # Checked BEFORE the metadata parse, and it has to stay that way on
            # the merged-stream backend: the cap keeps the stream's tail, so an
            # overflowing response has lost the head of the page and may have
            # lost the status line with it. "Half a page" and "no metadata" are
            # one fault there, and the size limit is its honest name.
            raise ValueError(f"response exceeds {self.max_bytes} byte limit")
        marker = prefix.encode("utf-8")
        # Both stream shapes, one read. With split streams the line is on
        # stderr and the stdout cut finds nothing, so the body comes through
        # untouched; with merged streams (the shipped sandbox ExecEnv, which
        # always reports ``stderr=b""``) the line rides inside the body,
        # wherever curl's buffering put it, and is cut back out.
        body, merged_meta = _split_curl_meta(outcome.stdout, marker)
        _, split_meta = _split_curl_meta(outcome.stderr, marker)
        meta = split_meta if split_meta is not None else merged_meta
        if meta is None:
            raise RuntimeError(
                "webfetch curl produced no status metadata on either stream — "
                "the container's curl is too old for '-w' write-out "
                "(needs >= 7.63)"
            )
        status, redirect_url = meta
        return body.decode("utf-8", errors="replace"), status, redirect_url


def build_web_tools(
    exec_env: Optional[ExecEnv] = None,
    digester: Optional[PageDigester] = None,
    page_cache: Optional[PageCache] = None,
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
    ``"llm"`` backend); ``None`` keeps the raw-render behaviour. ``page_cache``
    is the task's :class:`PageCache` (the session pack reads it off the
    task's local slot); ``None`` gives the tool a private one.

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
        WebFetchTool(
            transport=fetch_transport,
            digester=digester,
            cache=page_cache if page_cache is not None else PageCache(),
        )
    ]
    search = build_web_search_tool(exec_env=exec_env)
    if search is not None:
        tools.append(search)
    return {t.name: t for t in tools}
