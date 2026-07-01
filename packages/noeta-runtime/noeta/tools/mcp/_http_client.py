"""Minimal synchronous HTTP JSON-RPC client for a remote MCP server.

The sibling of :class:`~noeta.tools.mcp._client.McpStdioClient`:
where the stdio client talks to a local subprocess over newline-delimited
JSON-RPC, this one talks to a **remote HTTP** endpoint — but it keeps the same
two Noeta commitments F2 fixed for stdio:

* **synchronous, single-threaded** (no asyncio, no ``mcp`` SDK, no background
  reader) — every call is a blocking ``POST`` that reads back exactly one
  JSON-RPC response object;
* **request-response subset only** (``initialize`` / ``tools/list`` /
  ``tools/call``) — never the server-push half of Streamable HTTP
  (``list_changed`` / ``sampling`` / ``elicitation``), so there is no long-lived
  stream to listen on and the conversation tool set stays frozen (D4).

Transport: ``urllib.request`` from the stdlib (no ``requests`` / ``httpx``
dependency). Each call POSTs a single JSON-RPC request and parses a single JSON
response. We accept either a bare JSON object (the simplest servers) or a
``text/event-stream`` body carrying one ``data:`` JSON line (the shape the MCP
Streamable HTTP spec returns even for a one-shot request-response); we read the
first JSON-RPC object whose ``id`` matches and stop — we never hold the stream
open to listen for pushes.

Credentials (D3/D5): static headers (a Bearer token / API key / custom header)
are injected here from the host-side config and **never** appear in any request
body, event, or recording. They ride only on the wire.

Caps (mirroring the stdio client): a per-call ``timeout`` and a response body
``total_cap`` (bounded memory). Every transport / protocol / timeout fault
raises :class:`~noeta.tools.mcp._client.McpError`, which the shared ``McpTool``
wrapper turns into a typed failed ``ToolResult``; at ``prepare`` time
(initialize / tools-list) it propagates as a fail-fast.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

from noeta.tools.mcp._client import (
    DEFAULT_MCP_TIMEOUT_S,
    DEFAULT_MCP_TOTAL_CAP,
    McpError,
)


__all__ = [
    "DEFAULT_MCP_HTTP_TIMEOUT_S",
    "McpHttpClient",
    "HttpPostFn",
]


DEFAULT_MCP_HTTP_TIMEOUT_S = DEFAULT_MCP_TIMEOUT_S
_PROTOCOL_VERSION = "2024-11-05"


#: The HTTP POST entrypoint. Injectable so tests can substitute a fake
#: transport (and prove resume NEVER reaches it). Takes the JSON-RPC request
#: object + the merged request headers; returns the raw response body bytes.
HttpPostFn = Callable[[dict[str, Any], Mapping[str, str]], bytes]


class McpHttpClient:
    """A synchronous request-response connection to one remote HTTP MCP server.

    ``url`` is the single JSON-RPC endpoint the server exposes; every method is
    POSTed there. ``headers`` are the static credential / custom headers merged
    onto every request (D5) — they are sent on the wire only, never recorded.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        timeout_s: float = DEFAULT_MCP_HTTP_TIMEOUT_S,
        total_cap: int = DEFAULT_MCP_TOTAL_CAP,
        post: Optional[HttpPostFn] = None,
    ) -> None:
        if not url:
            raise McpError("mcp http server url is empty")
        self._url = url
        self._headers = dict(headers or {})
        self._timeout_s = timeout_s
        self._total_cap = total_cap
        self._post = post or self._default_post
        self._next_id = 0
        self._started = False
        self._closed = False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Complete the MCP handshake (``initialize``). Fail-fast: any
        transport / protocol fault raises :class:`McpError`."""
        if self._started:
            raise McpError("client already started")
        self._started = True
        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "noeta", "version": "0"},
            },
        )

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpError("tools/list result missing 'tools' array")
        return [t for t in tools if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )

    def list_prompts(self) -> list[dict[str, Any]]:
        """Discover the server's prompts (``prompts/list``).

        Same request-response subset as the stdio client: one POST, one JSON-RPC
        response, never a server-push stream. Returns the raw
        ``[{name, description?, arguments?}]`` entries."""
        result = self._request("prompts/list", {})
        prompts = result.get("prompts")
        if not isinstance(prompts, list):
            raise McpError("prompts/list result missing 'prompts' array")
        return [p for p in prompts if isinstance(p, dict)]

    def get_prompt(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Expand one prompt (``prompts/get``) with arguments.

        Returns the raw result (``{description?, messages: [...]}``)."""
        return self._request(
            "prompts/get", {"name": name, "arguments": dict(arguments)}
        )

    def list_resources(self) -> list[dict[str, Any]]:
        """Discover the server's STATIC resources (``resources/list``).

        Same request-response subset as the rest of the client: one POST, one
        JSON-RPC response, never a server-push stream (no ``resources/updated``).
        Returns the raw ``[{uri, name?, description?, mimeType?}]`` entries — the
        v1 static clip-list only (resource templates / parameterised URIs are out
        of scope)."""
        result = self._request("resources/list", {})
        resources = result.get("resources")
        if not isinstance(resources, list):
            raise McpError("resources/list result missing 'resources' array")
        return [r for r in resources if isinstance(r, dict)]

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one resource (``resources/read``) by URI.

        Returns the raw result (``{contents: [{uri, mimeType?, text?, blob?}]}``);
        the caller flattens its text contents into the snapshot it records."""
        return self._request("resources/read", {"uri": uri})

    def shutdown(self) -> None:
        """No-op teardown (HTTP is stateless / connectionless here);
        idempotent and never raises. Present so callers can treat the HTTP
        and stdio clients uniformly."""
        self._closed = True

    # -- JSON-RPC over HTTP ---------------------------------------------

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        try:
            body = self._post(req, headers)
        except McpError:
            raise
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            raise McpError(f"{method} http error: {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"{method} url error: {exc.reason}") from exc
        except OSError as exc:
            raise McpError(f"{method} transport error: {exc}") from exc
        if len(body) > self._total_cap:
            raise McpError("server output exceeded total cap")
        msg = self._extract_response(method, body, req_id)
        if "error" in msg and msg["error"] is not None:
            raise McpError(f"{method} error: {msg['error']}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} result is not an object")
        return result

    def _extract_response(
        self, method: str, body: bytes, req_id: int
    ) -> dict[str, Any]:
        """Parse the JSON-RPC response from a raw body.

        Accepts a bare JSON object OR an SSE (``text/event-stream``) body whose
        ``data:`` lines carry JSON-RPC objects — we return the first object
        whose ``id`` matches our request and never read further (no push). A
        non-JSON / wrong-shape body raises :class:`McpError`."""
        text = body.decode("utf-8", errors="replace").strip()
        if not text:
            raise McpError(f"{method}: empty response body")
        # Fast path: a plain JSON object.
        if text[0] == "{":
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise McpError(f"{method}: malformed JSON response: {exc}") from exc
            if isinstance(obj, dict):
                return obj
            raise McpError(f"{method}: JSON-RPC response is not an object")
        # SSE path: scan ``data:`` lines for the matching JSON-RPC object.
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            # Compare ids as strings: a spec-compliant server may echo the
            # JSON-RPC id as a string ("1") while ``req_id`` is our int (1),
            # so a raw ``==`` would never match and the call would spuriously
            # raise below. Normalising both sides keeps int/str echoes matching.
            if isinstance(obj, dict) and str(obj.get("id")) == str(req_id):
                return obj
        raise McpError(f"{method}: no matching JSON-RPC response in body")

    def _default_post(
        self, req: dict[str, Any], headers: Mapping[str, str]
    ) -> bytes:
        data = json.dumps(req, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — url is operator config
            self._url, data=data, headers=dict(headers), method="POST"
        )
        with urllib.request.urlopen(  # noqa: S310 — operator-configured endpoint
            request, timeout=self._timeout_s
        ) as resp:
            return resp.read(self._total_cap + 1)
