"""Minimal synchronous stdio JSON-RPC client for a local MCP server.

The Model Context Protocol stdio transport is
newline-delimited JSON-RPC 2.0 (one JSON object per line on stdin /
stdout; the server's logs go to stderr). Noeta's runtime is synchronous
and single-threaded, so this is a deliberately tiny **sync** client — no
asyncio, no `mcp` SDK dependency — modelled on the subprocess discipline
in ``noeta.runtime.shell_policy``:

* launched with an argv list (never ``shell=True``) + a scrubbed env;
* stderr → ``DEVNULL`` so a chatty server can never fill a pipe and
  deadlock; no drain thread needed;
* stdout read with a ``select``-based per-call **timeout** and a
  per-line + cumulative byte **cap** (no unbounded memory);
* every transport / protocol / timeout fault raises :class:`McpError`
  (the ``McpTool`` wrapper turns that into a typed failed ``ToolResult``);
* ``shutdown`` is bounded: close stdin → terminate → wait → kill → reap,
  idempotent;
* one JSON-RPC exchange at a time: a connection is shared by every turn
  that names its server (``noeta.builtins.mcp.impl.pool``), so
  ``_request`` holds an instance lock from send to matched reply — two
  turns queue on one stdin instead of interleaving lines and swallowing
  each other's replies.

Request-response subset: ``initialize`` +
``notifications/initialized`` + ``tools/list`` + ``tools/call`` +
``prompts/list`` + ``prompts/get`` + ``resources/list`` +
``resources/read``; the three list calls follow ``nextCursor`` to the last
page. No streaming and no server-push half (``list_changed`` /
``sampling`` / ``elicitation``). A message carrying ``method`` is a server
request or notification, never our reply: a ``ping`` is answered with an
empty result, anything else is ignored.

A call timeout, an EOF, or a read fault leaves the pipe in an unknown state
(a late reply may still be in flight), so the connection marks itself
:attr:`McpStdioClient.broken` and every later request fails at once instead
of waiting out its own timeout; the tool wrapper retires it from the pool.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from noeta.runtime.env import scrub_env
from noeta.runtime.mcp import McpError


__all__ = [
    "MAX_LIST_PAGES",
    "collect_pages",
    "DEFAULT_MCP_TIMEOUT_S",
    "DEFAULT_MCP_LINE_CAP",
    "DEFAULT_MCP_TOTAL_CAP",
    "McpError",
    "McpStdioClient",
    "SpawnFn",
]


DEFAULT_MCP_TIMEOUT_S = 30.0
DEFAULT_MCP_LINE_CAP = 1 * 1024 * 1024  # 1 MB per JSON-RPC line
DEFAULT_MCP_TOTAL_CAP = 8 * 1024 * 1024  # 8 MB cumulative per call
_PROTOCOL_VERSION = "2024-11-05"
_MAX_INTERLEAVED_MESSAGES = 64  # notifications tolerated before a response
#: Upper bound on pages one list call follows — a server that keeps handing
#: back a cursor must not loop the build forever.
MAX_LIST_PAGES = 100


def collect_pages(
    request: Callable[[str, dict[str, Any]], dict[str, Any]],
    method: str,
    key: str,
) -> list[dict[str, Any]]:
    """Run a paginated MCP list call (``tools/list`` / ``prompts/list`` /
    ``resources/list``) to its last page and return every entry.

    Follows ``nextCursor`` until the server stops sending one, a cursor
    repeats, or :data:`MAX_LIST_PAGES` pages have been read. A page whose
    ``key`` is not an array is a shape fault (:class:`McpError`). Shared by
    the stdio and HTTP clients."""
    out: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    for _ in range(MAX_LIST_PAGES):
        result = request(method, {"cursor": cursor} if cursor else {})
        entries = result.get(key)
        if not isinstance(entries, list):
            raise McpError(f"{method} result missing {key!r} array")
        out.extend(e for e in entries if isinstance(e, dict))
        nxt = result.get("nextCursor")
        if not isinstance(nxt, str) or not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return out


#: The process-launch entrypoint. Injectable so tests can (a) substitute
#: a fake and (b) prove resume NEVER reaches it (the no-spawn sentinel).
SpawnFn = Callable[..., "subprocess.Popen[bytes]"]


def _default_spawn(argv: list[str], env: dict[str, str]) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(  # noqa: S603 — argv list, never shell=True
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        bufsize=0,
    )


class _PipeFault(McpError):
    """A fault that leaves the pipe unusable (timeout / EOF / read error);
    :meth:`McpStdioClient._request` marks the connection broken on it."""


class McpStdioClient:
    """A live connection to one stdio MCP server."""

    def __init__(
        self,
        *,
        argv: list[str],
        env: Optional[dict[str, str]] = None,
        timeout_s: float = DEFAULT_MCP_TIMEOUT_S,
        call_timeout_s: Optional[float] = None,
        line_cap: int = DEFAULT_MCP_LINE_CAP,
        total_cap: int = DEFAULT_MCP_TOTAL_CAP,
        spawn: Optional[SpawnFn] = None,
    ) -> None:
        if not argv:
            raise McpError("mcp server argv is empty")
        self._argv = list(argv)
        #: extra env vars merged ONTO the scrubbed base env at
        #: spawn (a front-end-configured stdio server may need e.g. an API key
        #: in env). Empty/None ⇒ the bare scrubbed env, byte-identical to F2.
        self._extra_env = dict(env or {})
        self._timeout_s = timeout_s
        #: ``tools/call`` budget; ``None`` ⇒ ``timeout_s``.
        self._call_timeout_s = call_timeout_s
        self._line_cap = line_cap
        self._total_cap = total_cap
        self._spawn = spawn or _default_spawn
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._readbuf = b""
        self._next_id = 0
        self._closed = False
        #: Why this connection may no longer be used (a timeout / EOF / read
        #: fault), or ``None`` while it is healthy.
        self._broken: Optional[str] = None
        # Serializes a whole request/reply exchange (and a notification), so
        # concurrent holders of one pooled connection never interleave on the
        # pipe. ``shutdown`` deliberately does NOT take it: it runs only once
        # no holder is left (pool) or at process exit, and a hung request must
        # not stall teardown for its full timeout.
        self._exchange_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def broken(self) -> bool:
        """True once a timeout / EOF / read fault left the pipe unusable;
        every later request then fails at once."""
        return self._broken is not None

    def start(self) -> None:
        """Spawn the server and complete the MCP handshake. Fail-fast:
        any spawn / initialize fault raises :class:`McpError`."""
        if self._proc is not None:
            raise McpError("client already started")
        env = scrub_env()
        if self._extra_env:
            env = {**env, **self._extra_env}
        try:
            self._proc = self._spawn(self._argv, env)
        except OSError as exc:
            raise McpError(f"spawn failed: {exc}") from exc
        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "noeta", "version": "0"},
            },
        )
        self._notify("notifications/initialized", {})

    def list_tools(self) -> list[dict[str, Any]]:
        return collect_pages(self._request, "tools/list", "tools")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout_s=self._call_timeout_s,
        )

    def list_prompts(self) -> list[dict[str, Any]]:
        """Discover the server's prompts (``prompts/list``).

        Returns the raw ``[{name, description?, arguments?}]`` entries (the
        request-response subset, no server-push). Fail-fast on a shape fault."""
        return collect_pages(self._request, "prompts/list", "prompts")

    def get_prompt(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Expand one prompt (``prompts/get``) with arguments.

        Returns the raw result (``{description?, messages: [...]}``); the caller
        flattens its messages into injectable text."""
        return self._request(
            "prompts/get", {"name": name, "arguments": dict(arguments)}
        )

    def list_resources(self) -> list[dict[str, Any]]:
        """Discover the server's STATIC resources (``resources/list``).

        Returns the raw ``[{uri, name?, description?, mimeType?}]`` entries (the
        request-response subset, no server-push). v1 static clip-list only —
        resource templates / parameterised URIs are out of scope. Fail-fast on a
        shape fault."""
        return collect_pages(self._request, "resources/list", "resources")

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one resource (``resources/read``) by URI.

        Returns the raw result (``{contents: [{uri, mimeType?, text?, blob?}]}``);
        the caller flattens its text contents into the recorded snapshot."""
        return self._request("resources/read", {"uri": uri})

    def shutdown(self) -> None:
        """Bounded teardown: close stdin → terminate → wait → kill →
        reap. Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        finally:
            for stream in (proc.stdout, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    # -- JSON-RPC --------------------------------------------------------

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        with self._exchange_lock:
            if self._broken is not None:
                raise McpError(f"{method}: connection unusable ({self._broken})")
            try:
                return self._request_locked(
                    method,
                    params,
                    self._timeout_s if timeout_s is None else timeout_s,
                )
            except _PipeFault as exc:
                self._broken = str(exc)
                raise

    def _request_locked(
        self, method: str, params: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        if self._proc is None:
            raise McpError("client not started")
        self._next_id += 1
        req_id = self._next_id
        self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + timeout_s
        # Per-request cumulative byte budget — bounds memory across the
        # whole call, not just one line. Any bytes already buffered (rest
        # carried over from a prior request) count against this request,
        # and every subsequent `os.read` accumulates; exceeding the total
        # cap raises regardless of how many small interleaved
        # notifications a server streams before the real response.
        consumed = [len(self._readbuf)]
        if consumed[0] > self._total_cap:
            raise McpError("server output exceeded total cap")
        # Read lines until the response with our id arrives; tolerate a
        # bounded number of interleaved notifications / other-id messages.
        for _ in range(_MAX_INTERLEAVED_MESSAGES):
            msg = self._recv_line(deadline, consumed)
            if "method" in msg:
                # A server request or notification — never our reply, even
                # when its id happens to equal ours.
                if msg.get("method") == "ping" and msg.get("id") is not None:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                continue
            if msg.get("id") != req_id:
                continue  # an unrelated reply — skip
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                raise McpError(f"{method} error: {err}")
            result = msg.get("result")
            if not isinstance(result, dict):
                raise McpError(f"{method} result is not an object")
            return result
        raise McpError(f"{method}: too many interleaved messages before response")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._exchange_lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("client stdin not available")
        line = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise _PipeFault(f"write failed: {exc}") from exc

    def _recv_line(self, deadline: float, consumed: list[int]) -> dict[str, Any]:
        """Return one decoded JSON object from stdout, honouring the
        deadline + caps. ``consumed`` is the per-request cumulative
        byte counter (single-element list): every ``os.read`` adds to it
        and exceeding ``total_cap`` raises, so a flood of small
        sub-line-cap notifications before the real response cannot grow
        memory without bound. Raises :class:`McpError` on timeout / EOF /
        oversize / malformed JSON."""
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        while b"\n" not in self._readbuf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _PipeFault("timeout waiting for server response")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise _PipeFault("timeout waiting for server response")
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                raise _PipeFault(f"read failed: {exc}") from exc
            if chunk == b"":
                raise _PipeFault("server closed stdout (process exited?)")
            self._readbuf += chunk
            consumed[0] += len(chunk)
            if consumed[0] > self._total_cap:
                raise McpError("server output exceeded total cap")
        line, _, rest = self._readbuf.partition(b"\n")
        self._readbuf = rest
        if len(line) > self._line_cap:
            raise McpError("server response line exceeded cap")
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpError(f"malformed JSON-RPC line: {exc}") from exc
        if not isinstance(obj, dict):
            raise McpError("JSON-RPC message is not an object")
        return obj
