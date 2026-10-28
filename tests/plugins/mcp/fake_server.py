"""A scripted stdio MCP server for tests (run as ``python fake_server.py``).

Behavior is steered by environment variables:

* ``FAKE_TOOLS``: JSON list of tool entries for ``tools/list`` (default: echo,
  fail, image). ``FAKE_PAGE``: page size (pagination with ``nextCursor``).
* ``FAKE_TOOLS_FILE``: a JSON file read on every ``tools/list`` instead.
* ``FAKE_LOG``: a file each received method is appended to.
* ``FAKE_PING``: send a ``ping`` request (with a colliding id) before each
  reply.

Server-to-client traffic: the ``ask`` tool sends ``elicitation/create``
(its arguments as the params) and returns the client's answer as JSON;
``roots`` asks ``roots/list`` and returns the answer; ``changed`` writes
``FAKE_CHANGE_TO`` (if set) into ``FAKE_TOOLS_FILE`` and sends
``notifications/tools/list_changed`` before its reply.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

TOOLS = json.loads(os.environ.get("FAKE_TOOLS") or "null") or [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "image", "description": "Returns a picture.", "inputSchema": {"type": "object"}},
    {"name": "sleep", "description": "Sleeps.", "inputSchema": {"type": "object"}},
    {"name": "die", "description": "Exits the server.", "inputSchema": {"type": "object"}},
    {"name": "pid", "description": "The server's process id.", "inputSchema": {"type": "object"}},
]
PAGE = int(os.environ.get("FAKE_PAGE") or 0)
LOG = os.environ.get("FAKE_LOG")
PING = bool(os.environ.get("FAKE_PING"))
AUDIO = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
#: A 1 x 1 grey PNG.
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNoAAAAggCBd81ytgAAAABJ"
       "RU5ErkJggg==")


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def client_request(method: str, params: dict) -> dict:
    """Ask the client something and wait for its answer."""
    send({"jsonrpc": "2.0", "id": f"s-{method}", "method": method, "params": params})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == f"s-{method}" and "method" not in msg:
            return msg.get("result") or {"error": msg.get("error")}
    sys.exit(0)


def page(items: list, cursor: str | None, key: str) -> dict:
    if not PAGE:
        return {key: items}
    start = int(cursor or 0)
    out: dict = {key: items[start:start + PAGE]}
    if start + PAGE < len(items):
        out["nextCursor"] = str(start + PAGE)
    return out


def handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "f"}}
    if method == "tools/list":
        tools = TOOLS
        if os.environ.get("FAKE_TOOLS_FILE"):
            with open(os.environ["FAKE_TOOLS_FILE"]) as fh:
                tools = json.load(fh)
        return page(tools, params.get("cursor"), "tools")
    if method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "echo":
            return {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}],
                    "structuredContent": {"pid": os.getpid()}}
        if name == "ask":
            answer = client_request("elicitation/create", args)
            return {"content": [{"type": "text", "text": json.dumps(answer, sort_keys=True)}]}
        if name == "roots":
            answer = client_request("roots/list", {})
            return {"content": [{"type": "text", "text": json.dumps(answer)}]}
        if name == "changed":
            if os.environ.get("FAKE_CHANGE_TO"):
                with open(os.environ["FAKE_TOOLS_FILE"], "w") as fh:
                    fh.write(os.environ["FAKE_CHANGE_TO"])
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            return {"content": [{"type": "text", "text": "changed"}]}
        if name == "pid":
            return {"content": [{"type": "text", "text": str(os.getpid())}]}
        if name == "fail":
            return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        if name == "image":
            return {"content": [{"type": "image", "mimeType": "image/png", "data": PNG},
                                {"type": "audio", "mimeType": "audio/wav", "data": AUDIO}]}
        if name == "sleep":
            time.sleep(float(args.get("seconds", 5)))
            return {"content": []}
        if name == "die":
            sys.exit(0)
        raise KeyError(name)
    if method == "prompts/list":
        return {"prompts": [{"name": "review", "description": "Review code",
                             "arguments": [{"name": "path", "required": True}]}]}
    if method == "prompts/get":
        return {"messages": [{"role": "user", "content": {
            "type": "text", "text": f"Review {params['arguments'].get('path')}"}}]}
    if method == "resources/list":
        return {"resources": [{"uri": "file:///notes.md", "name": "notes"}]}
    if method == "resources/read":
        return {"contents": [{"uri": params["uri"], "text": "the notes"}]}
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        if LOG:
            with open(LOG, "a") as fh:
                fh.write(f"{msg.get('method')} {os.getpid()}\n")
        if "id" not in msg or "method" not in msg:
            continue  # a notification, or our ping's answer
        if PING:
            send({"jsonrpc": "2.0", "id": msg["id"], "method": "ping"})
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
        try:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg["method"],
                                                                      msg.get("params") or {})})
        except KeyError as exc:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "error": {"code": -32601, "message": f"unknown {exc}"}})


if __name__ == "__main__":
    main()
