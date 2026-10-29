"""``WebFetch``: fetch a URL, render it to Markdown, and answer ``prompt``
against it with a digest model call — the model reads the answer, not the
page. With no digest available (or a failed one) the rendering itself is
returned. ``localhost`` and dotless host names are refused (Claude Code's
rule: a local server is reached with ``curl``); ``http://`` is upgraded to
``https://`` (loopback addresses excepted); a redirect to another host is
reported, not followed; pages are cached per task for 15 minutes. A page
longer than one call's window is read from ``offset`` on. Deferred."""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
import posixpath
import time
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.web.render import (
    FetchedPage,
    check_text_media,
    collapse_whitespace,
    decode_body,
    media_type_of,
    render,
)
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    format_size,
    limit_output,
    load_description,
    spill_path,
    truncate_bytes,
)

__all__ = [
    "CrossHostRedirect",
    "Digest",
    "FetchTransport",
    "HttpFetchTransport",
    "PageCache",
    "WebFetchTool",
    "render_digest_prompt",
]

#: The rendered page cap: the digest model's reading budget and the
#: raw-render fallback's inline ceiling.
INLINE_PAGE_MAX_CHARS = 100_000
_MAX_REDIRECT_HOPS = 10
#: The longest URL fetched.
MAX_URL_CHARS = 2000
CACHE_TTL_SECONDS = 900.0
CACHE_MAX_ENTRIES = 16
#: Claude Code's ``maxResultSizeChars`` for WebFetch.
WEBFETCH_RESULT_LIMIT = 50_000
_FETCH_SCHEMES = frozenset({"http", "https"})


DIGEST_INSTRUCTIONS = load_description("noeta.plugins.web", "webfetch_digest")


def render_digest_prompt(*, url: str, title: str, page_markdown: str, prompt: str,
                         instructions: str = DIGEST_INSTRUCTIONS) -> str:
    """The one user message the digest model sees."""
    return "\n".join([
        instructions.strip(), "", f"Request: {prompt}", "", f"Page title: {title}",
        f"Page URL: {url}", "", "Page content (Markdown):", page_markdown,
    ])


#: ``(url, title, page_markdown, prompt) -> answer``; raises on failure.
Digest = Callable[[str, str, str, str], Awaitable[str]]


class PageCache:
    """The 15-minute per-URL page cache (successes only) of one task. A
    binary body is cached as the note naming the file it was saved to
    (``binary=True``), never as text."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = CACHE_TTL_SECONDS,
                 max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self.clock = clock
        self._ttl = ttl_seconds
        self._max = max_entries
        self._pages: dict[str, tuple[float, str, str, bool]] = {}

    def get(self, url: str) -> tuple[str, str] | None:
        entry = self.entry(url)
        return None if entry is None else (entry[0], entry[1])

    def entry(self, url: str) -> tuple[str, str, bool] | None:
        """``(title, markdown, binary)`` for ``url``, or ``None``."""
        entry = self._pages.get(url)
        if entry is None:
            return None
        expires_at, title, markdown, binary = entry
        if self.clock() >= expires_at:
            del self._pages[url]
            return None
        return title, markdown, binary

    def put(self, url: str, title: str, markdown: str, *, binary: bool = False) -> None:
        self._pages.pop(url, None)
        while len(self._pages) >= self._max:
            del self._pages[next(iter(self._pages))]
        self._pages[url] = (self.clock() + self._ttl, title, markdown, binary)


def _is_loopback_url(url: str) -> bool:
    try:
        host = httpx.URL(url).host
    except Exception:  # noqa: BLE001 - a malformed URL fails at fetch time
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def upgrade_to_https(url: str) -> str:
    """``http://`` is fetched over ``https://``, except on loopback."""
    if url[:7].lower() != "http://" or _is_loopback_url(url):
        return url
    return "https://" + url[7:]


def unsupported_scheme_refusal(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if scheme and scheme not in _FETCH_SCHEMES:
        return f"WebFetch fetches http(s) URLs only; {scheme}: is not fetched"
    if host and (host == "localhost" or host.endswith(".localhost") or "." not in host):
        return (f"WebFetch does not fetch {host}: localhost and host names without a dot "
                "are refused. Reach a local server with curl through Bash.")
    return None


class CrossHostRedirect(RuntimeError):
    """A redirect that leaves the requested host: surfaced, never followed."""

    def __init__(self, url: str, location: str, status: int = 302) -> None:
        super().__init__(f"{url} redirects to a different host: {location}")
        self.url = url
        self.location = location
        self.status = status


#: Longest redirect target relayed to the model, and the longest host name
#: that can be fetched (a DNS name's limit).
_MAX_RELAYED_URL_CHARS = 1000
_MAX_HOST_CHARS = 255


def redirect_report(redirect: CrossHostRedirect, prompt: str, offset: int) -> str:
    """What the model is told of a redirect to another host: where from and
    to, the status, and the call that would fetch the target."""
    try:
        target = httpx.URL(redirect.location)
        fetchable = target.scheme in _FETCH_SCHEMES and bool(target.host)
    except Exception:  # noqa: BLE001 - an unusable target is reported as such
        fetchable = False
    location = collapse_whitespace(redirect.location)
    lines = [f"Not fetched: {redirect.url} redirects to another host.",
             f"Status: {redirect.status} {status_phrase(redirect.status)}"]
    if not fetchable:
        lines.append("Target: withheld, the server's Location header is not an http(s) URL. "
                     "It cannot be fetched from here; report the redirect.")
        return "\n".join(lines)
    shown = location[:_MAX_RELAYED_URL_CHARS]
    cut = len(location) - len(shown)
    lines.append(f"Target (the server's Location header, unverified): {shown}"
                 + (f" [{cut} more characters left out]" if cut else ""))
    if cut or len(target.host) > _MAX_HOST_CHARS:
        lines.append("That target cannot be fetched from here (too long to relay, or not a "
                     "valid host name); report the redirect.")
        return "\n".join(lines)
    call = f'To fetch it, call WebFetch again with:\n  url: "{shown}"\n  prompt: "{prompt}"'
    if offset > 0:
        call += f"\n  offset: {offset}"
    lines.append(call)
    return "\n".join(lines)


class HttpStatus(RuntimeError):
    """The server answered with a status that is not success: reported to
    the model as what the server said, not as a failed call."""

    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def status_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown Status"


def http_status_report(error: HttpStatus) -> str:
    """What the model is told of an error status."""
    retry = f"Retry-After: {error.retry_after}\n" if error.retry_after else ""
    return (f"The server answered HTTP {error.status} {status_phrase(error.status)}.\n{retry}"
            "Its body was not read. If the page needs a sign-in, fetch it with a tool that "
            "is signed in instead (`gh` for GitHub, or an MCP server's fetch tool).")


class FetchTransport(Protocol):
    """url -> page. Raises on transport failure, :class:`HttpStatus` on an
    error status, on a body that is not text, and :class:`CrossHostRedirect`
    on a redirect leaving the origin."""

    async def fetch(self, url: str) -> str | FetchedPage: ...


def _same_origin(a: str, b: str) -> bool:
    """Whether a redirect from ``a`` to ``b`` stays on the site: the same
    scheme and port, the same host up to a leading ``www.``, and no
    credentials in the target."""
    ua, ub = httpx.URL(a), httpx.URL(b)
    defaults = {"http": 80, "https": 443}

    def _port(u: httpx.URL) -> int | None:
        return u.port if u.port is not None else defaults.get(u.scheme)

    def _site(host: str) -> str:
        return host.removeprefix("www.")

    if ub.userinfo:
        return False
    return (ua.scheme, _site(ua.host), _port(ua)) == (ub.scheme, _site(ub.host), _port(ub))


class HttpFetchTransport:
    """Fetch over httpx: same-site redirects followed (10 hops), the body
    streamed and refused past ``max_bytes``."""

    def __init__(self, *, timeout: float = 60.0,
                 user_agent: str = "noeta-webfetch/0.1 (+https://github.com/initxy/noeta)",
                 client: httpx.AsyncClient | None = None,
                 max_bytes: int = 10 * 1024 * 1024) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.client = client
        self.max_bytes = max_bytes

    async def fetch(self, url: str) -> FetchedPage:
        client = self.client or httpx.AsyncClient(timeout=self.timeout)
        try:
            current = url
            for _ in range(_MAX_REDIRECT_HOPS + 1):
                async with client.stream("GET", current, follow_redirects=False,
                                         headers={"User-Agent": self.user_agent}) as resp:
                    if resp.is_redirect:
                        target = str(httpx.URL(current).join(resp.headers.get("location", "")))
                        if not _same_origin(current, target):
                            raise CrossHostRedirect(current, target, resp.status_code)
                        current = target
                        continue
                    if not resp.is_success:
                        retry = resp.headers.get("retry-after", "").strip()
                        raise HttpStatus(resp.status_code, retry if retry.isdigit()
                                         and len(retry) <= 6 else None)
                    content_type = resp.headers.get("content-type", "")
                    try:
                        check_text_media(current, content_type)
                        binary = False
                    except ValueError:
                        binary = True
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ValueError(f"response exceeds {self.max_bytes} byte limit")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    if binary:
                        return FetchedPage("", content_type, raw=body)
                    return FetchedPage(decode_body(body, content_type), content_type)
            raise RuntimeError(f"too many redirects (>{_MAX_REDIRECT_HOPS}) fetching {url}")
        finally:
            if self.client is None:
                await client.aclose()


def _valid_url(url: str) -> bool:
    """Claude Code's bounds on a URL: at most :data:`MAX_URL_CHARS`
    characters, and no user name or password in it."""
    if len(url) > MAX_URL_CHARS:
        return False
    try:
        parts = urlsplit(url)
        return parts.username is None and parts.password is None
    except ValueError:
        return False


def _offset(input: dict[str, Any]) -> int:
    raw = input.get("offset")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0


def _schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "The URL to fetch."},
            "prompt": {"type": "string",
                       "description": "What to find out from the fetched page."},
            "offset": {"type": "integer", "minimum": 0, "maximum": 9007199254740991,
                       "description": ("Character position in the page text to continue "
                                       "from, as given by the previous result, for a page "
                                       "too long for one call.")},
        },
        "required": ["url", "prompt"],
        "additionalProperties": False,
    }


class WebFetchTool:
    """``WebFetch``. ``digest_for(ctx)`` gives the call's digest function
    (``None``: return the rendering); ``cache_for(ctx)`` its task's cache."""

    name = "WebFetch"
    read_only = True
    retry_safe = True
    clearable = True
    deferred = True

    def __init__(self, transport: FetchTransport, *,
                 digest_for: Callable[[ToolContext], Digest | None] = lambda _c: None,
                 cache_for: Callable[[ToolContext], PageCache] | None = None,
                 digest_timeout: float = 60.0) -> None:
        self.transport = transport
        self.description = load_description("noeta.plugins.web", "webfetch")
        self.input_schema = _schema()
        self.digest_for = digest_for
        self._cache = PageCache()
        self.cache_for = cache_for or (lambda _c: self._cache)
        self.digest_timeout = digest_timeout

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        url = input.get("url")
        if not isinstance(url, str) or not url.strip():
            return text_output("WebFetch requires a non-empty 'url'", is_error=True)
        prompt = input.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return text_output(
                "WebFetch requires a non-empty 'prompt' — the question to "
                "answer against the fetched page", is_error=True)
        if not _valid_url(url):
            return text_output("Invalid URL", is_error=True)
        url = upgrade_to_https(url)
        refusal = unsupported_scheme_refusal(url)
        if refusal is not None:
            return text_output(refusal, is_error=True)
        cache = self.cache_for(ctx)
        cached = cache.entry(url)
        if cached is not None and cached[2]:
            return text_output(cached[1], url=url)
        if cached is not None:
            title, markdown, _ = cached
        else:
            try:
                fetched = await self.transport.fetch(url)
            except CrossHostRedirect as redirect:
                return text_output(redirect_report(redirect, prompt, _offset(input)),
                                   redirect=redirect.location[:_MAX_RELAYED_URL_CHARS])
            except HttpStatus as status:
                return text_output(http_status_report(status), url=url,
                                   status=status.status)
            except Exception as exc:  # noqa: BLE001 - degrade, don't crash the step
                return text_output(f"WebFetch failed: {exc}", is_error=True)
            raw = fetched.raw if isinstance(fetched, FetchedPage) else None
            if raw is not None:
                assert isinstance(fetched, FetchedPage)
                saved = await _save_binary(ctx, raw, fetched.content_type)
                media = media_type_of(fetched.content_type)
                if saved is None:
                    try:
                        check_text_media(url, fetched.content_type)
                    except ValueError as exc:
                        return text_output(f"WebFetch failed: {exc}", is_error=True)
                    return text_output(
                        f"WebFetch failed: the response is binary ({media}, "
                        f"{format_size(len(raw))}) and there is nowhere to save it",
                        is_error=True)
                # Not text: neither rendered nor summarized, only named.
                note = (f"[The response is binary ({media}, {format_size(len(raw))}); "
                        f"it was saved to {saved}]")
                cache.put(url, "", note, binary=True)
                return text_output(note, url=url)
            title, markdown = render(fetched)
            if not markdown.strip():
                return text_output(
                    f"WebFetch got no readable text from {truncate_bytes(url, 100)} — the page "
                    "rendered to empty text (blocked, empty, or script-only); "
                    "try another source", is_error=True)
            cache.put(url, title, markdown)

        start = _offset(input)
        total = len(markdown)
        if start >= total > 0:
            return text_output(f"Nothing is left to read from offset {start}: the page's text "
                               f"is {total} characters long.", url=url, chars=total)
        end = min(total, start + INLINE_PAGE_MAX_CHARS)
        page = markdown[start:end]
        more = (f"\n\n(The page continues: this covers characters {start}-{end} of {total}. "
                f"Call again with offset={end} for the rest.)") if end < total else ""
        meta: dict[str, Any] = {"url": url, "chars": total}
        digest = self.digest_for(ctx)
        if digest is None:
            body = f"{page}{more}"
        else:
            try:
                answer = await asyncio.wait_for(digest(url, title, page, prompt),
                                                self.digest_timeout)
            except Exception:  # noqa: BLE001 - a digest failure degrades to the raw render
                answer = ""
            if answer.strip():
                body = f"{answer}{more}"
                meta["digested"] = True
            else:
                body = f"(Digest unavailable — raw page rendering follows.)\n{page}{more}"
        body = await limit_output(body, WEBFETCH_RESULT_LIMIT, ctx)
        return text_output(body, **meta)


async def _save_binary(ctx: ToolContext, raw: bytes, content_type: str) -> str | None:
    """A binary body saved under the spill directory, for the model to open
    (with ``Read``); ``None`` when there is nowhere to save it."""
    if not ctx.spill_dir:
        return None
    ext = mimetypes.guess_extension(media_type_of(content_type)) or ".bin"
    base = posixpath.splitext(spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id))[0]
    path = f"{posixpath.dirname(base)}/webfetch-{posixpath.basename(base)}{ext}"
    try:
        await ctx.env.write_bytes(path, raw)
    except OSError:
        return None
    return path
