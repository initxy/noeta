"""The sign-ins the ``authenticate`` / ``complete_authentication`` tools of
the ``Mcp`` extension run (:mod:`noeta.plugins.mcp.oauth` does the OAuth
part): one per server and connection scope, in the worker that started it,
handed out again while it is fresh."""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from noeta.core.tool import ToolOutput, text_output
from noeta.plugins.mcp.config import McpError, McpServer
from noeta.plugins.mcp.oauth import SignIn, store_for
from noeta.plugins.mcp.pool import McpPool
from noeta.plugins.mcp.tools import McpAuthTool

log = logging.getLogger("noeta.plugins.mcp")

#: A sign-in started within this long is handed out again rather than
#: restarted.
SIGN_IN_REUSE_S = 180.0


@dataclass
class _SignInRun:
    sign_in: SignIn
    url: str
    started: float
    done: asyncio.Task[None]


class SignIns:
    """The sign-ins in progress, by (scope, server identity); ``pool`` is
    the extension's connection pool, which forgets a server's connection
    once its sign-in succeeds."""

    def __init__(self, pool: Callable[[], McpPool]) -> None:
        self._pool = pool
        self._runs: dict[tuple[object, ...], _SignInRun] = {}

    async def start(self, tool: McpAuthTool, server: McpServer,
                    scope: str | None) -> ToolOutput:
        """The ``authenticate`` tool: the URL the person opens."""
        key = self._pool().key(server, scope)
        complete = tool.name.removesuffix("authenticate") + "complete_authentication"
        run = self._runs.get(key)
        reused = run is not None and not run.done.done() \
            and time.monotonic() - run.started < SIGN_IN_REUSE_S
        if not reused:
            if run is not None:
                await run.sign_in.close()
            sign_in = SignIn(server, store_for(server, scope), http=self._pool().http)
            try:
                url = await sign_in.start()
            except McpError as exc:
                await sign_in.close()
                return text_output(f"Could not start the sign-in to {server.alias!r}: {exc}. "
                                   "Tell the user it is not available right now.",
                                   is_error=True)
            run = _SignInRun(sign_in, url, time.monotonic(),
                             asyncio.create_task(self._await(server, scope, sign_in)))
            self._runs[key] = run
        assert run is not None
        earlier = ("A sign-in started earlier is still waiting, so this is its link. "
                   if reused else "")
        return text_output(
            f"{earlier}Ask the user to open this URL in their browser to authorize the "
            f"{server.alias!r} MCP server:\n{run.url}\n\nOnce they have, its tools become "
            "available from their next message. If the browser shows a connection error on "
            "the page it is sent back to, ask them for the full URL in the address bar and "
            f"call `{complete}` with it.")

    async def _await(self, server: McpServer, scope: str | None, sign_in: SignIn) -> None:
        try:
            await sign_in.finish()
        except McpError as exc:
            log.warning("mcp: the sign-in to %s failed: %s", server.alias, exc)
            raise
        await self._pool().forget(server, scope)

    async def finish(self, tool: McpAuthTool, server: McpServer, scope: str | None,
                     args: dict[str, Any]) -> ToolOutput | None:
        """The ``complete_authentication`` tool: hand over the callback URL
        and wait for the sign-in. ``None`` once it succeeded; what to tell
        the model otherwise."""
        start = tool.name.removesuffix("complete_authentication") + "authenticate"
        run = self._runs.get(self._pool().key(server, scope))
        if run is None or (run.done.done() and run.done.exception() is not None):
            return text_output(f"No sign-in to {server.alias!r} is in progress. Call `{start}` "
                               "first, then retry with the callback URL.", is_error=True)
        url = str(args.get("callback_url") or "")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "code" not in query and "error" not in query:
            return text_output("That URL carries no authorization code. Ask the user for the "
                               "full address from the browser's address bar, including the "
                               "`?code=...&state=...` part.", is_error=True)
        if not run.done.done() and not run.sign_in.submit(url):
            return text_output(f"That URL belongs to another sign-in attempt to "
                               f"{server.alias!r}; the current one is still waiting. Ask the "
                               "user for the URL from the page this sign-in opened.",
                               is_error=True)
        try:
            await asyncio.shield(run.done)
        except McpError as exc:
            return text_output(f"The sign-in to {server.alias!r} failed: {exc}", is_error=True)
        return None
