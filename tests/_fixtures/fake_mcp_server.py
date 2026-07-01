"""A tiny fake stdio MCP server for F2 tests.

Speaks the newline-delimited JSON-RPC 2.0 subset Noeta's
``McpStdioClient`` uses: ``initialize`` / ``notifications/initialized`` /
``tools/list`` / ``tools/call``. The first argv selects a behaviour
``mode`` so tests can exercise discovery, the echo happy path, an
``isError`` result, a sanitize collision, a slow (timeout) call, a server
that dies mid-call, and an oversized output line.

Run unbuffered: ``[python, "-u", this_file, mode]``.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def _respond(mid: object, result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def _tools_for(mode: str) -> list[dict[str, Any]]:
    if mode == "collision":
        # Two distinct raw names that sanitize to the same Noeta name `mcp__x__a_b`.
        return [
            {"name": "a.b", "inputSchema": {"type": "object"}},
            {"name": "a/b", "inputSchema": {"type": "object"}},
        ]
    if mode == "empty_name":
        return [{"name": "", "inputSchema": {"type": "object"}}]
    if mode == "multi":
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
    if mode == "envcheck":
        # Reflects an env var back so a test can prove env reaches the spawn.
        import os
        return [
            {"name": "seen", "description": os.environ.get("FAKE_TOKEN", ""),
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


def _handle_call(mid: object, mode: str, params: dict[str, Any]) -> None:
    args = params.get("arguments") or {}
    if mode == "flood":
        # Stream many small (sub-line-cap) notification lines BEFORE the
        # real response — each below line_cap, but cumulatively well over
        # a small total_cap. Exercises the per-request byte budget.
        for _ in range(40):
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"data": "x" * 50_000},
                    }
                )
                + "\n"
            )
        sys.stdout.flush()
        _respond(mid, {"content": [{"type": "text", "text": "ok"}]})
        return
    if mode == "error":
        _respond(mid, {"content": [{"type": "text", "text": "boom"}], "isError": True})
        return
    if mode == "slow":
        time.sleep(5.0)
        _respond(mid, {"content": [{"type": "text", "text": "late"}]})
        return
    if mode == "die":
        sys.exit(0)  # exit without responding → client sees EOF
    if mode == "bigline":
        big = "x" * (2 * 1024 * 1024)
        _respond(mid, {"content": [{"type": "text", "text": big}]})
        return
    _respond(
        mid,
        {"content": [{"type": "text", "text": json.dumps(args, sort_keys=True)}]},
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    if mode == "die_init":
        sys.exit(0)  # exit before answering initialize → handshake fail-fast
    for raw in iter(sys.stdin.readline, ""):
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _respond(
                mid,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0"},
                },
            )
        elif method == "notifications/initialized":
            continue  # a notification — no response
        elif method == "tools/list":
            _respond(mid, {"tools": _tools_for(mode)})
        elif method == "tools/call":
            _handle_call(mid, mode, msg.get("params") or {})
        # unknown methods are ignored


if __name__ == "__main__":
    main()
