"""The stdio MCP client.

The stdio transport is newline-delimited JSON-RPC 2.0 on the server's
stdin/stdout; its logs go to stderr, which is discarded so a chatty server can
never fill a pipe.

* The server is launched from an argv list (never a shell) with a scrubbed
  environment plus the spec's ``env``, in its own process group.
* A pooled connection is shared by every task that names the server:
  requests are multiplexed by id (:class:`~noeta.plugins.mcp.client.StreamClient`),
  so one slow call (or one waiting on the person) never holds up another.
* A line is capped; an oversized line, EOF or a read fault leaves the pipe
  in an unknown state, so the connection marks itself ``broken``; the pool
  then retires it and the next use connects afresh. A malformed line is
  skipped. A request that times out is cancelled and the pipe stays usable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from noeta.core.execenv import scrub_env
from noeta.plugins.mcp.client import (
    MAX_LIST_PAGES,
    PROTOCOL_VERSION,
    McpClient,
    StreamClient,
    collect_pages,
    initialize_params,
)
from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpError

__all__ = [
    "LINE_CAP",
    "MAX_LIST_PAGES",
    "PROTOCOL_VERSION",
    "TOTAL_CAP",
    "McpClient",
    "StdioClient",
    "collect_pages",
    "initialize_params",
]

log = logging.getLogger("noeta.mcp")

LINE_CAP = 1024 * 1024
TOTAL_CAP = 8 * 1024 * 1024


class StdioClient(StreamClient):
    """A live connection to one local stdio server."""

    def __init__(self, argv: Sequence[str], *, env: dict[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, line_cap: int = LINE_CAP,
                 bare_elicitation: bool = False) -> None:
        if not argv:
            raise McpError("mcp server argv is empty")
        super().__init__(timeout=timeout, bare_elicitation=bare_elicitation)
        self.argv = list(argv)
        self.extra_env = dict(env or {})
        self.line_cap = line_cap
        self.proc: asyncio.subprocess.Process | None = None
        self._write_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    async def _open(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**scrub_env(), **self.extra_env},
                limit=self.line_cap + 1,
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(f"spawn failed: {exc}") from exc

    async def _write(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpError("client not started")
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise McpError("server response line exceeded cap") from exc
            except OSError as exc:
                raise McpError(f"read failed: {exc}") from exc
            if not line:
                raise McpError("server closed stdout (process exited?)")
            if len(line) > self.line_cap:
                raise McpError("server response line exceeded cap")
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("mcp: skipped a malformed line from %s", self.argv[0])
                continue
            if isinstance(obj, dict):
                yield obj

    async def _shut(self) -> None:
        """Close stdin, terminate, wait, kill: bounded."""
        proc = self.proc
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 2.0)
