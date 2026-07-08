"""`webfetch` tool — fetch a public URL, render its HTML body to Markdown.

`webfetch(url)` pulls a page over HTTP(S) through an injected
:class:`FetchTransport`, converts the HTML body to a compact Markdown rendering
with a small in-tree heuristic (headings → ``#``, anchors → ``[text](href)``,
list items → ``- ``, paragraphs preserved; script/style/head stripped, tags
otherwise dropped, entities unescaped, whitespace collapsed), offloads the
**full** rendering to a ContentStore artifact, and returns a small inline
object ``{url, title, content, content_ref, truncated}``.

``webfetch`` has ``risk_level="low"`` (a read-only GET; no workspace mutation).

Private / authenticated URLs cannot be reached without credentials: the server
answers 401/403 (or the host is unreachable), the transport raises, and the
tool degrades to ``ToolResult(success=False, ...)`` with a message that names
the cause — it never raises out of the step. This limitation is stated in the
tool's description resource so the model does not try webfetch on intranet /
logged-in pages.

The Markdown conversion is a deliberately minimal, dependency-free heuristic; it
is deterministic given identical input bytes so a resumed run reproduces the same
artifact.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import httpx

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    SUMMARY_EMBED_MAX_BYTES,
    encoded_len,
    fit_output_fields,
    truncate_bytes,
)
from noeta.tools.descriptions import load_tool_description
from noeta.tools.fs.exec_env import ExecEnv
from noeta.tools.web.search import _outcome_error_text, build_web_search_tool


__all__ = [
    "FetchTransport",
    "HttpFetchTransport",
    "ContainerCurlFetchTransport",
    "WebFetchTool",
    "build_web_tools",
]


_FETCH_MEDIA_TYPE = "text/markdown"
_MAX_URL_BYTES = 512
_MAX_TITLE_BYTES = 200

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


class FetchTransport(Protocol):
    """A url → raw page text seam. Raises on transport / HTTP failure."""

    def fetch(self, url: str) -> str: ...


@dataclass
class WebFetchTool:
    """Fetch a public URL and return its body rendered to Markdown."""

    transport: FetchTransport
    name: str = "webfetch"
    description: str = field(default=load_tool_description("webfetch"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            return ToolResult(
                success=False, summary="webfetch requires a non-empty 'url'"
            )
        try:
            raw = self.transport.fetch(url)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the step
            return ToolResult(success=False, summary=f"webfetch failed: {exc}")

        title = _extract_title(raw)
        markdown = html_to_markdown(raw)
        # The full Markdown is the artifact; a bounded view rides inline. The
        # model can deref the ref for the rest of a long page.
        ref = ctx.artifact_store.put(
            markdown.encode("utf-8"), media_type=_FETCH_MEDIA_TYPE
        )
        output: dict[str, Any] = {
            "url": truncate_bytes(url, _MAX_URL_BYTES),
            "title": truncate_bytes(title, _MAX_TITLE_BYTES),
            "content": markdown,
            "content_ref": {
                "hash": ref.hash,
                "size": ref.size,
                "media_type": ref.media_type,
            },
            "truncated": False,
        }
        # Hard canonical-encoded ceiling: if the inline ``content`` does not
        # fit, shrink it to an excerpt and mark truncated (the full body is the
        # artifact; the model derefs ``content_ref`` for the rest).
        if encoded_len(output) > INLINE_CONTENT_MAX_BYTES:
            output["truncated"] = True
            output = fit_output_fields(
                output,
                shrink_order=["content", "title", "url"],
                max_bytes=INLINE_CONTENT_MAX_BYTES,
            )
        summary_url = truncate_bytes(url, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            artifacts=[ref],
            summary=f"fetched {summary_url} ({ref.size}B markdown)",
        )


@dataclass
class HttpFetchTransport:
    """Real HTTP fetch over httpx.

    ``client`` is injectable so tests pass an ``httpx.Client`` backed by an
    ``httpx.MockTransport`` (no live network). ``raise_for_status`` turns a
    private / authenticated URL's 401/403 into a clear ``HTTPStatusError`` that
    the tool surfaces as a failed ``ToolResult``.
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
            with client.stream(
                "GET",
                url,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as resp:
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
        finally:
            if self.client is None:
                client.close()


@dataclass
class ContainerCurlFetchTransport:
    """Fetch a URL through the sandbox container via ``curl`` (D6).

    In sandbox mode (D3/D5) a tool's execution must land inside the session's
    container rather than on the host, so ``webfetch`` egresses by running
    ``curl`` through the ``ExecEnv`` process seam instead of streaming over
    httpx. The fetched HTML is handed to the SAME :func:`html_to_markdown` the
    httpx path uses — only the transport moves into the container.

    ``--fail`` makes ``curl`` exit nonzero on an HTTP >= 400 (a private /
    authenticated URL answering 401/403) — WITHOUT it ``curl`` returns exit 0
    and hands back the error page as if it were the body, so the tool would
    surface a server error page as a "successful" fetch. With ``--fail`` such a
    response (or a timeout) raises with the cause named and the tool degrades to
    ``ToolResult(success=False, ...)`` — byte-for-byte the same outcome the httpx
    path reaches through ``raise_for_status`` (R3: the two egress paths cannot
    diverge on HTTP error status).
    """

    exec_env: ExecEnv
    cwd: Path = Path("/")
    timeout: float = 10.0
    user_agent: str = HttpFetchTransport.user_agent
    #: Ceiling on the fetched body, mirroring ``HttpFetchTransport.max_bytes``:
    #: the container ``run_argv`` caps captured output at this size.
    max_bytes: int = 5 * 1024 * 1024

    def fetch(self, url: str) -> str:
        argv = [
            "curl",
            "-sSL",
            # Parity with httpx ``raise_for_status``: a 4xx/5xx must fail the run,
            # not return the error page as a "successful" body (see class doc).
            "--fail",
            "--max-time",
            str(int(self.timeout)),
            "-A",
            self.user_agent,
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
        return outcome.stdout.decode("utf-8", errors="replace")


def build_web_tools(exec_env: Optional[ExecEnv] = None) -> dict[str, Tool]:
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

    When ``exec_env`` is supplied (sandbox mode) both tools egress THROUGH the
    container — ``webfetch`` via :class:`ContainerCurlFetchTransport` and
    ``web_search`` via :class:`ContainerCurlSearchTransport` — instead of over
    httpx on the host (D3/D6). ``exec_env is None`` keeps the byte-identical
    host httpx path.
    """
    fetch_transport: FetchTransport = (
        ContainerCurlFetchTransport(exec_env=exec_env)
        if exec_env is not None
        else HttpFetchTransport()
    )
    tools: list[Tool] = [WebFetchTool(transport=fetch_transport)]
    search = build_web_search_tool(exec_env=exec_env)
    if search is not None:
        tools.append(search)
    return {t.name: t for t in tools}
