"""`web_search` tool — run a web search, return ranked hits as compact Markdown.

`web_search(query, count?)` issues a query through an injected
:class:`SearchTransport`, renders the ranked hits (each ``title`` / ``url`` /
``snippet``) into a compact Markdown list, offloads the **full** rendering to a
ContentStore artifact, and returns a small inline object
``{query, count, content, content_ref, truncated}``. It is ``webfetch``'s sibling
in the web pack: a read-only lookup with no workspace mutation, so
``risk_level="low"``.

The real backend (:class:`HttpSearchTransport`) calls the Tavily Search API
(``POST https://api.tavily.com/search``, Bearer-authenticated). The key is read
from ``NOETA_WEB_SEARCH_API_KEY`` at build time: with no key the tool is not
constructed at all (``build_web_tools`` omits it, like an MCP server that fails
to connect), so the model never sees a search tool it cannot use.

A missing / empty query, a transport / HTTP failure, or an empty result set
degrade to ``ToolResult(success=False, ...)`` with a message that names the
cause — the tool never raises out of the step.

The Markdown rendering is deterministic given identical hits, so a resumed run
reproduces the same artifact.
"""

from __future__ import annotations

import json
import os
import secrets
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
from noeta.tools.fs._subprocess import _RunOutcome
from noeta.tools.fs.exec_env import ExecEnv


__all__ = [
    "SearchResult",
    "SearchTransport",
    "HttpSearchTransport",
    "ContainerCurlSearchTransport",
    "WebSearchTool",
]


#: Environment variable carrying the search backend's API key. Its presence is
#: the on/off switch for the whole tool (see ``build_web_tools`` in fetch.py).
SEARCH_API_KEY_ENV = "NOETA_WEB_SEARCH_API_KEY"

_SEARCH_MEDIA_TYPE = "text/markdown"
_MAX_QUERY_BYTES = 512
_MAX_TITLE_BYTES = 200
_MAX_URL_BYTES = 512
_MAX_SNIPPET_BYTES = 1000
#: Default / clamp bounds on the requested hit count (the backend caps it too).
_DEFAULT_COUNT = 5
_MAX_COUNT = 20
#: Ceiling on a container ``curl`` response body (bytes) — the sandbox
#: ``run_argv`` caps captured output the same way ``HttpFetchTransport`` bounds a
#: streamed body; a Tavily result set is far smaller than this.
_CONTAINER_OUTPUT_CAP = 5 * 1024 * 1024


def _outcome_error_text(outcome: "_RunOutcome") -> str:
    """A human cause string for a failed container ``curl`` run.

    Prefers ``stderr`` (where ``curl -sS`` writes its error), falling back to
    ``stdout`` because the AIO backend merges both streams into ``stdout`` (see
    :class:`~noeta.tools.fs.exec_env.AioSandboxExecEnv`). Shared by both web
    container transports so their failure messages read the same.
    """
    stderr = outcome.stderr.decode("utf-8", errors="replace").strip()
    if stderr:
        return stderr
    return outcome.stdout.decode("utf-8", errors="replace").strip()


@dataclass(frozen=True)
class SearchResult:
    """One ranked hit: a page title, its URL, and a short snippet."""

    title: str
    url: str
    snippet: str


def _clamp_count(raw: Any) -> int:
    """Coerce the model-supplied ``count`` into ``[1, _MAX_COUNT]`` (default on junk)."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return _DEFAULT_COUNT
    if raw < 1:
        return 1
    if raw > _MAX_COUNT:
        return _MAX_COUNT
    return raw


def results_to_markdown(results: list[SearchResult]) -> str:
    """Render ranked hits to a compact, deterministic Markdown list.

    Each hit becomes ``N. [title](url)`` followed by its snippet on the next
    line. Same hits → same output (a resumed run reproduces it).
    """
    blocks: list[str] = []
    for i, r in enumerate(results, start=1):
        title = r.title.strip() or r.url.strip()
        line = f"{i}. [{title}]({r.url.strip()})"
        snippet = r.snippet.strip()
        if snippet:
            line += f"\n   {snippet}"
        blocks.append(line)
    return "\n\n".join(blocks)


def _parse_tavily_payload(payload: dict) -> list[SearchResult]:
    """Extract ranked :class:`SearchResult` hits from a Tavily response body.

    Shared by both transports (:class:`HttpSearchTransport` over httpx and
    :class:`ContainerCurlSearchTransport` over the sandbox ``curl``) so the two
    network paths cannot drift in how a Tavily payload maps to hits (R3): only
    the transport differs, the parse is one place. Each field is length-clamped
    exactly as the pre-seam inline extraction did.
    """
    results: list[SearchResult] = []
    for item in payload.get("results", []) or []:
        results.append(
            SearchResult(
                title=truncate_bytes(str(item.get("title", "")), _MAX_TITLE_BYTES),
                url=truncate_bytes(str(item.get("url", "")), _MAX_URL_BYTES),
                # Tavily names the per-hit excerpt ``content``.
                snippet=truncate_bytes(
                    str(item.get("content", "")), _MAX_SNIPPET_BYTES
                ),
            )
        )
    return results


class SearchTransport(Protocol):
    """A query → ranked hits seam. Raises on transport / HTTP failure."""

    def search(self, query: str, count: int) -> list[SearchResult]: ...


@dataclass
class WebSearchTool:
    """Run a web search and return ranked hits rendered to Markdown."""

    transport: SearchTransport
    name: str = "web_search"
    description: str = field(default=load_tool_description("web_search"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                success=False, summary="web_search requires a non-empty 'query'"
            )
        count = _clamp_count(arguments.get("count"))
        try:
            results = self.transport.search(query, count)
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the step
            return ToolResult(success=False, summary=f"web_search failed: {exc}")

        if not results:
            return ToolResult(
                success=False, summary=f"web_search found no results for {query!r}"
            )

        markdown = results_to_markdown(results)
        # The full rendering is the artifact; a bounded view rides inline. The
        # model can deref the ref for the rest of a long result list.
        ref = ctx.artifact_store.put(
            markdown.encode("utf-8"), media_type=_SEARCH_MEDIA_TYPE
        )
        output: dict[str, Any] = {
            "query": truncate_bytes(query, _MAX_QUERY_BYTES),
            "count": len(results),
            "content": markdown,
            "content_ref": {
                "hash": ref.hash,
                "size": ref.size,
                "media_type": ref.media_type,
            },
            "truncated": False,
        }
        # Hard canonical-encoded ceiling: if the inline ``content`` does not
        # fit, shrink it to an excerpt and mark truncated (the full list is the
        # artifact; the model derefs ``content_ref`` for the rest).
        if encoded_len(output) > INLINE_CONTENT_MAX_BYTES:
            output["truncated"] = True
            output = fit_output_fields(
                output,
                shrink_order=["content", "query"],
                max_bytes=INLINE_CONTENT_MAX_BYTES,
            )
        summary_query = truncate_bytes(query, SUMMARY_EMBED_MAX_BYTES)
        return ToolResult(
            success=True,
            output=output,
            artifacts=[ref],
            summary=f"searched {summary_query!r} ({len(results)} hits)",
        )


@dataclass
class HttpSearchTransport:
    """Real web search over the Tavily Search API (httpx).

    ``POST https://api.tavily.com/search`` with the query in a JSON body and the
    key as a ``Bearer`` token. ``client`` is injectable so tests pass an
    ``httpx.Client`` backed by an ``httpx.MockTransport`` (no live network).
    ``raise_for_status`` turns an auth / quota failure into a clear
    ``HTTPStatusError`` that the tool surfaces as a failed ``ToolResult``.
    """

    api_key: str
    endpoint: str = "https://api.tavily.com/search"
    timeout: float = 10.0
    client: Optional[httpx.Client] = None

    def search(self, query: str, count: int) -> list[SearchResult]:
        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            resp = client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "max_results": count},
            )
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if self.client is None:
                client.close()
        return _parse_tavily_payload(payload)


@dataclass
class ContainerCurlSearchTransport:
    """Web search over the Tavily API issued through the sandbox container.

    In sandbox mode (D3/D5) every tool's execution must happen inside the
    session's container rather than on the host. This transport reuses the
    ``ExecEnv`` process seam to run ``curl`` *inside* the container — the same
    ``POST https://api.tavily.com/search`` request ``HttpSearchTransport`` makes
    over httpx, only the egress moves into the sandbox (D6). The Tavily response
    is parsed by the SAME :func:`_parse_tavily_payload` the httpx path uses, so
    the two egress paths cannot drift (R3).

    ``--fail`` makes ``curl`` exit nonzero on an HTTP >= 400 (an auth / quota
    failure) rather than returning the error JSON with exit 0 — without it the
    error body parses to zero hits and the tool would silently degrade a 401 /
    429 to a bland "no results" instead of surfacing the cause. With ``--fail``
    such a response (or a timeout) raises (named cause) so the tool degrades to
    ``ToolResult(success=False, ...)`` exactly like the httpx ``raise_for_status``
    path (R3).

    The Tavily key never enters ``curl``'s argv (it would land in the container
    process table and any shell-command log the AIO backend keeps, D5). Instead
    the ``Authorization`` header is written to a ``curl --config`` file over the
    file API (``/v1/file/write`` — not a shell command) and referenced with
    ``-K``; the file is removed immediately after the request. ``curl``'s argv
    then carries only the config path, never the credential.
    """

    exec_env: ExecEnv
    api_key: str
    endpoint: str = "https://api.tavily.com/search"
    cwd: Path = Path("/")
    timeout: float = 10.0

    def search(self, query: str, count: int) -> list[SearchResult]:
        body = json.dumps({"query": query, "max_results": count})
        # Keep the Tavily key off the process table / shell log: the auth header
        # rides in a curl --config file written over the (non-shell) file API,
        # referenced by -K, and removed right after. A random name avoids a
        # collision between sibling subtasks sharing this session's container.
        config_path = Path(f"/tmp/.noeta-tavily-{secrets.token_hex(8)}.curl")
        self.exec_env.write_bytes(
            config_path,
            f'header = "Authorization: Bearer {self.api_key}"\n'.encode("utf-8"),
        )
        argv = [
            "curl",
            "-sS",
            # Parity with httpx ``raise_for_status``: a 4xx/5xx must fail the run
            # so an auth / quota error is surfaced, not degraded to "no results".
            "--fail",
            "-K",
            str(config_path),
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            self.endpoint,
        ]
        try:
            outcome = self.exec_env.run_argv(
                argv,
                cwd=self.cwd,
                timeout_s=int(self.timeout) + 5,
                output_cap=_CONTAINER_OUTPUT_CAP,
            )
        finally:
            # Best-effort: the container is per-session and short-lived, but drop
            # the credential file immediately so it is not readable at rest.
            try:
                self.exec_env.unlink(config_path)
            except Exception:  # noqa: BLE001 — cleanup must not mask the result
                pass
        if outcome.timed_out:
            raise RuntimeError(
                f"web_search curl timed out after {self.timeout}s: "
                f"{_outcome_error_text(outcome)}"
            )
        if outcome.returncode != 0:
            raise RuntimeError(
                f"web_search curl failed (exit {outcome.returncode}): "
                f"{_outcome_error_text(outcome)}"
            )
        try:
            payload = json.loads(outcome.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"web_search: malformed Tavily response: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("web_search: Tavily response is not an object")
        return _parse_tavily_payload(payload)


def build_web_search_tool(exec_env: Optional[ExecEnv] = None) -> Optional[Tool]:
    """Construct the ``web_search`` tool over the real backend, or ``None``.

    The key in ``NOETA_WEB_SEARCH_API_KEY`` is the on/off switch: with no key the
    tool cannot reach any backend, so we return ``None`` and the web pack omits
    it entirely (the model never sees a search tool it cannot use) — the same
    "skip on no connection" shape as a failed MCP server.

    When ``exec_env`` is supplied (sandbox mode) the request egresses through the
    container via :class:`ContainerCurlSearchTransport`; otherwise it goes out on
    the host over httpx (:class:`HttpSearchTransport`). The on/off gate and the
    tool's model-facing contract are identical either way.
    """
    api_key = os.environ.get(SEARCH_API_KEY_ENV, "").strip()
    if not api_key:
        return None
    if exec_env is not None:
        return WebSearchTool(
            transport=ContainerCurlSearchTransport(exec_env=exec_env, api_key=api_key)
        )
    return WebSearchTool(transport=HttpSearchTransport(api_key=api_key))
