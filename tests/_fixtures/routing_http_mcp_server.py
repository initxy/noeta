"""A multi-server fake HTTP MCP transport for the MCP-connectors tests.

The SDK host / CLI runner inject a SINGLE ``mcp_http_post`` for ALL enabled HTTP
servers, so a "one server fails, the rest work" test needs a router that
dispatches per server. We route on a per-server marker header (each server is
configured with a distinct ``X-Server`` header so the router knows which one a
request belongs to). A server in ``fail`` mode raises on EVERY call (so its
``initialize`` handshake fails → that server is skipped), while a server in
``echo`` / ``multi`` mode behaves like :class:`FakeHttpMcpServer`.

Reuses :class:`FakeHttpMcpServer` per route so the JSON-RPC subset / SSE / tool
shapes stay identical to the issue-01 fixture.
"""

from __future__ import annotations

from typing import Any, Mapping

from tests._fixtures.fake_http_mcp_server import FakeHttpMcpServer


class RoutingHttpMcpServer:
    """Dispatch one ``HttpPostFn`` across several per-alias fake servers.

    ``routes`` maps a marker value (the ``X-Server`` header each spec carries)
    to the mode that server runs in. ``post`` is the single ``HttpPostFn`` the
    host/runner injects; it reads the marker header and forwards to the matching
    per-route :class:`FakeHttpMcpServer` (so ``seen_headers`` / ``calls`` are
    tracked per route for assertions).
    """

    def __init__(self, routes: dict[str, str]) -> None:
        # marker -> FakeHttpMcpServer (its own mode)
        self._servers: dict[str, FakeHttpMcpServer] = {
            marker: FakeHttpMcpServer(mode=mode) for marker, mode in routes.items()
        }
        self.seen_headers: list[dict[str, str]] = []

    def server(self, marker: str) -> FakeHttpMcpServer:
        return self._servers[marker]

    def post(self, req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
        self.seen_headers.append(dict(headers))
        marker = headers.get("X-Server")
        if marker is None or marker not in self._servers:
            raise OSError(f"routing: no server for marker {marker!r}")
        return self._servers[marker].post(req, headers)
