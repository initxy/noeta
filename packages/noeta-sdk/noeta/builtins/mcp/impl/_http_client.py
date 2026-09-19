"""Minimal synchronous HTTP JSON-RPC client for a remote MCP server.

The sibling of :class:`~noeta.builtins.mcp.impl._client.McpStdioClient`:
where the stdio client talks to a local subprocess over newline-delimited
JSON-RPC, this one talks to a **remote HTTP** endpoint — but it keeps the same
two Noeta commitments F2 fixed for stdio:

* **synchronous, single-threaded** (no asyncio, no ``mcp`` SDK, no background
  reader) — every call is a blocking ``POST`` that reads back exactly one
  JSON-RPC response object;
* **request-response subset only** (``initialize`` / ``tools/list`` /
  ``tools/call``) — never the server-push half of Streamable HTTP
  (``list_changed`` / ``sampling`` / ``elicitation``), so there is no long-lived
  stream to listen on and the conversation tool set stays frozen.

Transport: ``urllib.request`` from the stdlib (no ``requests`` / ``httpx``
dependency). Each call POSTs a single JSON-RPC request and parses a single JSON
response. We accept either a bare JSON object (the simplest servers) or a
``text/event-stream`` body carrying one ``data:`` JSON line (the shape the MCP
Streamable HTTP spec returns even for a one-shot request-response); we read the
first JSON-RPC object whose ``id`` matches and stop — we never hold the stream
open to listen for pushes.

Sessions: a Streamable HTTP server (MCP 2025-03-26 / 2025-06-18) may assign an
``Mcp-Session-Id`` on ``initialize`` and then reject any later request that does
not carry it. This client captures that id, finishes the lifecycle with the
``notifications/initialized`` the spec asks for, echoes the id on every request
it makes afterwards, and ``DELETE``\\ s it when the connection is torn down —
still request/response only, with no stream held open. A server that assigns no
id is stateless and sees exactly the requests it saw before, notification
included.

Credentials: static headers (a Bearer token / API key / custom header)
are injected here from the host-side config and **never** appear in any request
body, event, or recording. The assigned session id is treated the same way — it
rides on the wire and is never logged or recorded.

Caps (mirroring the stdio client): a per-call ``timeout`` and a response body
``total_cap`` (bounded memory). Every transport / protocol / timeout fault
raises :class:`~noeta.runtime.mcp.McpError`, which the shared ``McpTool``
wrapper turns into a typed failed ``ToolResult``; at ``prepare`` time
(initialize / tools-list) it propagates as a fail-fast.
"""

from __future__ import annotations

import threading
import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional, Union

from noeta.builtins.mcp.impl._client import (
    DEFAULT_MCP_TIMEOUT_S,
    DEFAULT_MCP_TOTAL_CAP,
)
from noeta.runtime.mcp import HttpPostFn, McpError, McpHttpResponse


__all__ = [
    "DEFAULT_MCP_HTTP_TIMEOUT_S",
    "McpHttpClient",
    "HttpPostFn",
]


DEFAULT_MCP_HTTP_TIMEOUT_S = DEFAULT_MCP_TIMEOUT_S
_PROTOCOL_VERSION = "2024-11-05"

#: The Streamable HTTP session header. The server assigns an id on
#: ``initialize``, the client echoes it on every later request of that
#: connection, a ``DELETE`` carrying it ends the session, and a ``404`` while
#: one is in play means the server dropped it.
_CONNECTION_ID_HEADER = "Mcp-Session-Id"

#: How much of a ``DELETE`` response body to drain before closing it.
_DELETE_BODY_CAP = 4096

#: Teardown is bounded the way the stdio client's is: a server that stopped
#: answering must not hold up a retire, a host shutdown, or a finalizer, and
#: the call's own timeout (30 s by default) is a call budget, not a goodbye.
_DELETE_TIMEOUT_S = 5.0


def _assigned_id(headers: Mapping[str, str]) -> Optional[str]:
    """The connection id ``headers`` assigns, if any.

    Header names are case-insensitive on the wire and a transport may hand
    back a plain ``dict``, so the comparison is folded rather than a lookup.
    """
    wanted = _CONNECTION_ID_HEADER.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            text = str(value).strip()
            return text or None
    return None


def _reply_parts(
    reply: Union[bytes, McpHttpResponse],
) -> tuple[bytes, Optional[str], int]:
    """Normalise what a transport returned into ``(body, assigned id, status)``.

    A transport that returns raw ``bytes`` — the shape every host-supplied
    ``HttpPostFn`` had before sessions existed — carries no headers, so its
    connection stays stateless exactly as it was.
    """
    if isinstance(reply, McpHttpResponse):
        return reply.body, _assigned_id(reply.headers), reply.status
    return bytes(reply), None, 200


class McpHttpClient:
    """A synchronous request-response connection to one remote HTTP MCP server.

    ``url`` is the single JSON-RPC endpoint the server exposes; every method is
    POSTed there. ``headers`` are the static credential / custom headers merged
    onto every request — they are sent on the wire only, never recorded.

    When the server assigns a connection id on ``initialize`` (the Streamable
    HTTP session header), this instance holds it for as long as it lives and
    echoes it on every later request; the id belongs to the connection, which
    is why the pool's ``retire`` / ``shutdown`` is what ends it.
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
        # Only our own transport can send the session-ending ``DELETE``: a
        # host that injected one owns its network path (proxy, auth, mTLS),
        # and going around it with a bare ``urlopen`` would be wrong.
        self._owns_transport = post is None
        self._next_id = 0
        self._started = False
        self._closed = False
        # Exchanges may overlap on a pooled connection: the lock guards the
        # request-id counter and the id the server assigned this connection
        # (written once, on the initialize reply, and read on every request).
        self._connection_id: Optional[str] = None
        self._id_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Complete the MCP handshake (``initialize``, then the lifecycle's
        ``notifications/initialized``). Fail-fast: any transport / protocol
        fault raises :class:`McpError`, the notification included — the stdio
        client gives it the same fail-fast, and a handshake the server did not
        accept is a dead connection, which the caller already knows how to
        skip or retire."""
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
        if self._connection_id is not None:
            # Sent only when the server opened a session. The lifecycle asks
            # for it unconditionally, but this ships as a patch: a server that
            # assigns no id keeps a byte-identical wire, and a stateless server
            # has no state for the notification to advance anyway. A stateful
            # one may gate every later request on it, so there it is required.
            self._notify("notifications/initialized", {})

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
        """End the connection: idempotent, never raises.

        A stateless connection (the server assigned no id) has nothing to tear
        down, as before. One holding a session id sends a best-effort
        ``DELETE`` carrying it — the spec's way of ending a session instead of
        leaving the server to time it out. A ``405`` (the server does not allow
        clients to terminate) or any transport fault is ignored, because
        teardown runs on paths that must not fail: the pool retiring a
        connection, an idle expiry, host shutdown, a finalizer.
        """
        with self._id_lock:
            was_closed = self._closed
            self._closed = True
            held_id = self._connection_id
            self._connection_id = None
        if was_closed or held_id is None or not self._owns_transport:
            return
        try:
            self._default_delete(held_id)
        except Exception:  # noqa: BLE001 — teardown is best-effort by contract
            pass

    # -- JSON-RPC over HTTP ---------------------------------------------

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._id_lock:
            self._next_id += 1
            req_id = self._next_id
        body = self._exchange(
            method,
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
        )
        if len(body) > self._total_cap:
            raise McpError("server output exceeded total cap")
        msg = self._extract_response(method, body, req_id)
        if "error" in msg and msg["error"] is not None:
            raise McpError(f"{method} error: {msg['error']}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} result is not an object")
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """POST one JSON-RPC notification — no ``id``, so no reply to match.

        The spec answers a notification with ``202 Accepted`` and an empty
        body, so there is nothing to parse and an empty body is not a fault
        here (it is in :meth:`_request`). Transport and status faults raise
        :class:`McpError` as they do for a request."""
        self._exchange(method, {"jsonrpc": "2.0", "method": method, "params": params})

    def _exchange(self, method: str, req: dict[str, Any]) -> bytes:
        """POST one JSON-RPC message and hand back the raw response body.

        The shared half of a request and a notification: the credential and
        session headers go on here, every transport fault becomes an
        :class:`McpError`, a status a transport reported instead of raising
        fails the call, and an id the server assigns is picked up."""
        with self._id_lock:
            held_id = self._connection_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if held_id is not None:
            headers[_CONNECTION_ID_HEADER] = held_id
        try:
            reply = self._post(req, headers)
        except McpError:
            raise
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            self._note_status(exc.code, held_id)
            raise McpError(f"{method} http error: {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"{method} url error: {exc.reason}") from exc
        except OSError as exc:
            raise McpError(f"{method} transport error: {exc}") from exc
        body, assigned_id, status = _reply_parts(reply)
        if status >= 400:
            # A transport that returns the status instead of raising still has
            # to fail the call — and a 404 still means the session is gone.
            self._note_status(status, held_id)
            raise McpError(f"{method} http error: {status}")
        if assigned_id is not None:
            with self._id_lock:
                if self._connection_id is None:
                    self._connection_id = assigned_id
        return body

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

    def _note_status(self, status: int, held_id: Optional[str]) -> None:
        """React to a failing HTTP status before the fault is raised.

        ``404`` while a session id is in play is the server saying that
        session is gone (the spec's answer to a terminated or expired id).
        Forgetting the id is all this client does about it: no ``DELETE``
        chases a dead session, and the ``McpError`` the caller is about to see
        travels the path a dead connection already travels — the build retires
        the pooled connection and reconnects it once, which runs ``initialize``
        again and is assigned a fresh id. A 404 on a tool call surfaces as a
        failed ``ToolResult`` and is healed at the next build the same way.
        """
        if status == 404 and held_id is not None:
            with self._id_lock:
                if self._connection_id == held_id:
                    self._connection_id = None

    def _default_post(
        self, req: dict[str, Any], headers: Mapping[str, str]
    ) -> McpHttpResponse:
        data = json.dumps(req, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 — url is operator config
            self._url, data=data, headers=dict(headers), method="POST"
        )
        with urllib.request.urlopen(  # noqa: S310 — operator-configured endpoint
            request, timeout=self._timeout_s
        ) as resp:
            body = resp.read(self._total_cap + 1)
            return McpHttpResponse(
                body=body,
                headers={k: v for k, v in resp.headers.items()},
                status=int(getattr(resp, "status", 200) or 200),
            )

    def _default_delete(self, connection_id: str) -> None:
        """``DELETE`` the session id — the spec's explicit end of a session.

        Raises whatever the transport raises; :meth:`shutdown` is the one
        caller and swallows it.
        """
        request = urllib.request.Request(  # noqa: S310 — url is operator config
            self._url,
            headers={**self._headers, _CONNECTION_ID_HEADER: connection_id},
            method="DELETE",
        )
        with urllib.request.urlopen(  # noqa: S310 — operator-configured endpoint
            request, timeout=min(self._timeout_s, _DELETE_TIMEOUT_S)
        ) as resp:
            resp.read(_DELETE_BODY_CAP)
