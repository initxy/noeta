"""Minimal HTTP MCP discovery client for the connector management API.

Backs the ``GET .../mcp/servers/{alias}/tools|prompts|resources`` menus: it
connects to a configured HTTP connector, performs the MCP handshake
(``initialize``) and one ``*/list`` call, and returns the menu — so the config
UI can show every advertised tool and let the user tick a subset.

Why this lives in the app: the SDK exposes the per-turn wiring seam
(``HostConfig.mcp_server_resolver`` + the spec types) but deliberately does
NOT export its live MCP client machinery. Discovery is an application concern
(a management-surface read), so the app speaks the same synchronous
request-response JSON-RPC subset over ``httpx`` (which the app already
depends on) instead of importing runtime internals.

Protocol parity with the engine's own HTTP MCP client:

* synchronous request-response only — every call is one blocking POST that
  reads back exactly one JSON-RPC response object; never the server-push half
  of Streamable HTTP;
* the response may be a bare JSON object or a one-shot ``text/event-stream``
  body whose ``data:`` lines carry JSON-RPC objects — the first object whose
  ``id`` matches is taken, ids compared as strings (a spec-compliant server
  may echo an int id as a string);
* credential headers are injected on every request and never appear in any
  response, event, or error message.

Every transport / protocol fault raises :class:`noeta.sdk.McpError`, which
the API layer maps to a 502 (same error mapping as the retired app).

stdio connectors have no discovery here: spawning operator-configured
subprocesses from a management GET is not a surface this multi-user server
offers (the SDK connects stdio connectors inside the turn machinery; the tool
subset can still be set by name). The API maps that to a 400.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

import httpx

from noeta.sdk import McpError

__all__ = [
    "discover_prompts",
    "discover_resources",
    "discover_tools",
]

_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT_S = 15.0
#: Response body cap (bounded memory; a menu larger than this is a
#: misbehaving server, not a real catalog).
_TOTAL_CAP = 2 * 1024 * 1024


class _McpHttpSession:
    """One synchronous JSON-RPC conversation with a remote HTTP MCP server."""

    def __init__(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not url:
            raise McpError("mcp http server url is empty")
        self._url = url
        self._headers = dict(headers)
        self._timeout_s = timeout_s
        self._next_id = 0

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "noeta-agent", "version": "0"},
            },
        )

    def list_of(self, method: str, key: str) -> list[dict[str, Any]]:
        """One ``*/list`` call → the raw entry dicts under ``key``."""
        result = self._request(method, {})
        items = result.get(key)
        if not isinstance(items, list):
            raise McpError(f"{method} result missing '{key}' array")
        return [item for item in items if isinstance(item, dict)]

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        req_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        try:
            response = httpx.post(
                self._url,
                content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers=headers,
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            raise McpError(f"{method} transport error: {exc}") from exc
        if response.status_code >= 400:
            raise McpError(
                f"{method} http error: {response.status_code} "
                f"{response.reason_phrase}"
            )
        body = response.content
        if len(body) > _TOTAL_CAP:
            raise McpError("server output exceeded total cap")
        message = _extract_response(method, body, req_id)
        if message.get("error") is not None:
            raise McpError(f"{method} error: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} result is not an object")
        return result


def _extract_response(method: str, body: bytes, req_id: int) -> dict[str, Any]:
    """Parse the JSON-RPC response from a raw body (bare JSON or a one-shot
    SSE body; ids compared as strings)."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        raise McpError(f"{method}: empty response body")
    if text[0] == "{":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpError(f"{method}: malformed JSON response: {exc}") from exc
        if isinstance(obj, dict):
            return obj
        raise McpError(f"{method}: JSON-RPC response is not an object")
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
        if isinstance(obj, dict) and str(obj.get("id")) == str(req_id):
            return obj
    raise McpError(f"{method}: no matching JSON-RPC response in body")


def _connect(
    url: str, headers: Mapping[str, str], timeout_s: Optional[float]
) -> _McpHttpSession:
    session = _McpHttpSession(
        url, headers, timeout_s=timeout_s or _DEFAULT_TIMEOUT_S
    )
    session.initialize()
    return session


def discover_tools(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_s: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Connect + list the connector's FULL tool menu.

    Returns ``[{name, description}]`` name-sorted (deterministic display).
    The menu always shows every advertised tool — the stored subset filters
    turn-time tool building, never this catalog."""
    session = _connect(url, headers, timeout_s)
    tools = session.list_of("tools/list", "tools")
    menu = [
        {
            "name": str(t.get("name", "")),
            "description": str(t.get("description", "")),
        }
        for t in tools
        if t.get("name")
    ]
    return sorted(menu, key=lambda t: t["name"])


def discover_prompts(
    alias: str,
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_s: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Connect + list the connector's prompts.

    Returns ``[{name, noeta_name, description, arguments}]`` — ``noeta_name``
    is the ``mcp__<alias>__<name>`` slash-command token (the same naming the
    engine gives the connector's tools)."""
    session = _connect(url, headers, timeout_s)
    prompts = session.list_of("prompts/list", "prompts")
    menu: list[dict[str, Any]] = []
    for p in prompts:
        name = str(p.get("name", ""))
        if not name:
            continue
        arguments = p.get("arguments")
        menu.append(
            {
                "name": name,
                "noeta_name": f"mcp__{alias}__{name}",
                "description": str(p.get("description", "")),
                "arguments": arguments if isinstance(arguments, list) else [],
            }
        )
    return sorted(menu, key=lambda p: str(p["name"]))


def discover_resources(
    alias: str,
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_s: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Connect + list the connector's STATIC resources.

    Returns ``[{uri, name, description, mime_type, noeta_ref}]`` —
    ``noeta_ref`` is the ``<alias>:<uri>`` mention token."""
    session = _connect(url, headers, timeout_s)
    resources = session.list_of("resources/list", "resources")
    menu: list[dict[str, Any]] = []
    for r in resources:
        uri = str(r.get("uri", ""))
        if not uri:
            continue
        menu.append(
            {
                "uri": uri,
                "name": str(r.get("name", "")),
                "description": str(r.get("description", "")),
                "mime_type": str(r.get("mimeType", "")),
                "noeta_ref": f"{alias}:{uri}",
            }
        )
    return sorted(menu, key=lambda r: str(r["uri"]))
