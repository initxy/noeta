"""A tiny in-process fake remote HTTP MCP server for the MCP-connectors tests.

Returns an :class:`noeta.tools.mcp.HttpPostFn` — a callable
``(jsonrpc_request, headers) -> response_bytes`` — that the synchronous
:class:`noeta.tools.mcp.McpHttpClient` POSTs to instead of hitting a real
network. It speaks the request-response JSON-RPC subset the client uses
(``initialize`` / ``tools/list`` / ``tools/call``) and records the headers it
saw on each call so tests can assert the credential header was injected on the
wire (and never anywhere else).

A ``mode`` selects a behaviour the same way the stdio
``tests/_fixtures/fake_mcp_server.py`` does: ``echo`` (happy path), ``error``
(``isError`` result), ``sse`` (return the response as a ``text/event-stream``
body), and ``boom`` (raise so the client surfaces a transport fault).
"""

from __future__ import annotations

import json
from typing import Any, Mapping


class FakeHttpMcpServer:
    """An in-process fake whose :meth:`post` is an ``HttpPostFn``.

    ``seen_headers`` accumulates the headers of every request so a test can
    prove the configured credential header rode on the wire.
    """

    def __init__(self, *, mode: str = "echo") -> None:
        self.mode = mode
        self.seen_headers: list[dict[str, str]] = []
        self.calls: list[dict[str, Any]] = []

    def _tools(self) -> list[dict[str, Any]]:
        if self.mode == "multi":
            # Three tools so the per-server subset filter has
            # something to keep and something to drop.
            return [
                {"name": "alpha", "description": "tool alpha",
                 "inputSchema": {"type": "object"}},
                {"name": "beta", "description": "tool beta",
                 "inputSchema": {"type": "object"}},
                {"name": "gamma", "description": "tool gamma",
                 "inputSchema": {"type": "object"}},
            ]
        return [
            {
                "name": "echo",
                "description": "echo the arguments back",
                "inputSchema": {
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                },
            }
        ]

    def _prompts(self) -> list[dict[str, Any]]:
        """One parameterised prompt (``summarize``) the menu shows.

        Declares a required ``topic`` arg + an optional ``tone`` arg so a test
        can render a form, fill it, and assert the expansion echoes them back."""
        return [
            {
                "name": "summarize",
                "description": "Summarize a topic",
                "arguments": [
                    {
                        "name": "topic",
                        "description": "what to summarize",
                        "required": True,
                    },
                    {"name": "tone", "description": "voice", "required": False},
                ],
            }
        ]

    def _resources(self) -> list[dict[str, Any]]:
        """One static resource the unified ``@`` selector lists.

        Declares a ``uri`` + ``name`` + ``mimeType`` so a test can list it, build
        the ``<alias>:<uri>`` mention token, and read it (the ``resources/read``
        path returns the snapshot text)."""
        return [
            {
                "uri": "mem://notes/readme",
                "name": "readme",
                "description": "project readme",
                "mimeType": "text/plain",
            }
        ]

    def _result_for(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-http", "version": "0"},
            }
        if method == "tools/list":
            return {"tools": self._tools()}
        if method == "prompts/list":
            # A server with no prompts surface raises (the client treats
            # ``no_prompts`` mode as "prompts/list errors") — exercised by the
            # optional-capability path in the SDK helper.
            if self.mode == "no_prompts":
                return {}  # missing 'prompts' array → McpError in the client
            return {"prompts": self._prompts()}
        if method == "prompts/get":
            args = params.get("arguments") or {}
            name = params.get("name", "")
            topic = args.get("topic", "")
            tone = args.get("tone", "")
            text = f"Please summarize {topic}"
            if tone:
                text += f" in a {tone} tone"
            return {
                "description": f"expanded {name}",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": text},
                    }
                ],
            }
        if method == "resources/list":
            # A server with no resources surface raises (the client treats
            # ``no_resources`` mode as "resources/list errors") — exercises the
            # optional-capability path in the SDK helper.
            if self.mode == "no_resources":
                return {}  # missing 'resources' array → McpError in the client
            return {"resources": self._resources()}
        if method == "resources/read":
            uri = params.get("uri", "")
            return {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": f"SNAPSHOT of {uri}",
                    }
                ]
            }
        if method == "tools/call":
            args = params.get("arguments") or {}
            if self.mode == "error":
                return {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                }
            return {
                "content": [
                    {"type": "text", "text": json.dumps(args, sort_keys=True)}
                ]
            }
        return {}

    def post(self, req: dict[str, Any], headers: Mapping[str, str]) -> bytes:
        self.seen_headers.append(dict(headers))
        self.calls.append(dict(req))
        if self.mode == "boom":
            raise OSError("simulated transport failure")
        method = req.get("method", "")
        params = req.get("params") or {}
        result = self._result_for(method, params)
        envelope = {"jsonrpc": "2.0", "id": req.get("id"), "result": result}
        body = json.dumps(envelope).encode("utf-8")
        if self.mode == "sse" and method != "initialize":
            # Wrap the SAME JSON-RPC object in a one-event text/event-stream body,
            # exercising the client's SSE ``data:`` parsing path.
            return (
                b"event: message\n"
                + b"data: "
                + body
                + b"\n\n"
            )
        return body
