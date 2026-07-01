"""Minimal synchronous stdio JSON-RPC client for a local MCP server.

Phase 4.5 F2. The Model Context Protocol stdio transport is
newline-delimited JSON-RPC 2.0 (one JSON object per line on stdin /
stdout; the server's logs go to stderr). Noeta's runtime is synchronous
and single-threaded, so this is a deliberately tiny **sync** client — no
asyncio, no `mcp` SDK dependency — modelled on the subprocess discipline
in ``noeta.tools.fs.shell``:

* launched with an argv list (never ``shell=True``) + a scrubbed env;
* stderr → ``DEVNULL`` so a chatty server can never fill a pipe and
  deadlock; no drain thread needed;
* stdout read with a ``select``-based per-call **timeout** and a
  per-line + cumulative byte **cap** (no unbounded memory);
* every transport / protocol / timeout fault raises :class:`McpError`
  (the ``McpTool`` wrapper turns that into a typed failed ``ToolResult``);
* ``shutdown`` is bounded: close stdin → terminate → wait → kill → reap,
  idempotent.

Request-response subset: ``initialize`` +
``notifications/initialized`` + ``tools/list`` + ``tools/call`` +
``prompts/list`` + ``prompts/get`` (D9) + ``resources/list`` +
``resources/read`` (D10). No streaming, no server→client requests (we
advertise no sampling/roots capability), no server-push half
(``list_changed`` / ``sampling`` / ``elicitation``).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any, Callable, Optional

from noeta.tools._env import scrub_env


__all__ = [
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


class McpError(Exception):
    """A transport / protocol / timeout fault talking to an MCP server.

    Always caught by ``McpTool.invoke`` and turned into a typed failed
    ``ToolResult`` (never propagates out of a tool call). At ``prepare``
    time (spawn / initialize / tools-list) it propagates as a fail-fast.
    """


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


class McpStdioClient:
    """A live connection to one stdio MCP server."""

    def __init__(
        self,
        *,
        argv: list[str],
        env: Optional[dict[str, str]] = None,
        timeout_s: float = DEFAULT_MCP_TIMEOUT_S,
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
        self._line_cap = line_cap
        self._total_cap = total_cap
        self._spawn = spawn or _default_spawn
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._readbuf = b""
        self._next_id = 0
        self._closed = False

    # -- lifecycle -------------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

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

        Returns the raw ``[{name, description?, arguments?}]`` entries (the
        request-response subset, no server-push). Fail-fast on a shape fault."""
        result = self._request("prompts/list", {})
        prompts = result.get("prompts")
        if not isinstance(prompts, list):
            raise McpError("prompts/list result missing 'prompts' array")
        return [p for p in prompts if isinstance(p, dict)]

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
        result = self._request("resources/list", {})
        resources = result.get("resources")
        if not isinstance(resources, list):
            raise McpError("resources/list result missing 'resources' array")
        return [r for r in resources if isinstance(r, dict)]

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

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            raise McpError("client not started")
        self._next_id += 1
        req_id = self._next_id
        self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + self._timeout_s
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
            if msg.get("id") != req_id:
                continue  # a notification or unrelated message — skip
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                raise McpError(f"{method} error: {err}")
            result = msg.get("result")
            if not isinstance(result, dict):
                raise McpError(f"{method} result is not an object")
            return result
        raise McpError(f"{method}: too many interleaved messages before response")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("client stdin not available")
        line = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"write failed: {exc}") from exc

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
                raise McpError("timeout waiting for server response")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise McpError("timeout waiting for server response")
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                raise McpError(f"read failed: {exc}") from exc
            if chunk == b"":
                raise McpError("server closed stdout (process exited?)")
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
