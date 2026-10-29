"""What a front end's MCP management (``noeta mcp``) asks of the plugin, on
servers given as ``(name, spec)`` in Claude Code's ``mcpServers`` format.
Front ends reach it through the registry (``Mcp.admin``), never by import.
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from noeta.plugins.mcp.config import McpAuthRequired, McpConfigError, McpError, McpServer
from noeta.plugins.mcp.oauth import SignIn, store_for
from noeta.plugins.mcp.pool import McpPool
from noeta.plugins.mcp.settings import servers_from

__all__ = ["McpAdmin"]

#: How long a check waits for a server to answer.
CHECK_TIMEOUT_S = 15.0


def _server(name: str, spec: Mapping[str, Any]) -> McpServer:
    servers = servers_from({"mcpServers": {name: spec}}, name)
    if not servers:
        raise ValueError(f"MCP server {name!r} is not valid")
    return servers[0]


class McpAdmin:
    """Validation, health checks and sign-ins for configured servers.
    ``home`` is noeta's home (where sign-ins are kept)."""

    @staticmethod
    def transport(name: str, spec: Mapping[str, Any]) -> str:
        """The server's transport; ``ValueError`` when the spec is not
        valid."""
        return _server(name, spec).transport

    @staticmethod
    async def check(name: str, spec: Mapping[str, Any]) -> str:
        """``connected``, ``needs authentication`` or ``failed: <why>``."""
        try:
            server = _server(name, spec)
        except ValueError as exc:
            return f"failed: {exc}"
        pool = McpPool()
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_S):
                await pool.use(server, None, lambda c: c.list_tools())
        except McpAuthRequired:
            return "needs authentication"
        except (McpError, McpConfigError, OSError) as exc:
            return f"failed: {exc}"
        except TimeoutError:
            return "failed: no answer"
        finally:
            await pool.close()
        return "connected"

    @staticmethod
    def save_client_secret(name: str, spec: Mapping[str, Any], secret: str,
                           home: Path) -> None:
        """Keep the OAuth client secret of ``spec``'s ``oauth.clientId`` with
        the server's sign-in (never in a settings file)."""
        client_id = str((spec.get("oauth") or {}).get("clientId") or "")
        store_for(_server(name, spec), home=home).update(
            client={"client_id": client_id, "client_secret": secret})

    @staticmethod
    async def login(name: str, spec: Mapping[str, Any], home: Path, *,
                    show: Callable[[str], None], open_browser: bool = True,
                    pasted: Callable[[], Awaitable[str]] | None = None) -> None:
        """Sign in: ``show`` the authorization URL (and open a browser), then
        wait for the browser's redirect, or for the redirect URL ``pasted``
        returns. Raises ``McpError`` when it fails or times out."""
        server = _server(name, spec)
        if server.transport not in ("http", "sse"):
            raise McpError(f"MCP server {name!r} ({server.transport}) does not sign in "
                           "with OAuth")
        sign_in = SignIn(server, store_for(server, home=home))
        waiting: asyncio.Task[None] | None = None
        try:
            show(await sign_in.start())
            if open_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(sign_in.url)
            if pasted is not None:
                async def take() -> None:
                    url = (await pasted()).strip()
                    if url and not sign_in.submit(url):
                        show("That URL is not from this sign-in.")

                waiting = asyncio.create_task(take())
            await sign_in.finish()
        finally:
            if waiting is not None:
                waiting.cancel()
                with contextlib.suppress(BaseException):
                    await waiting
            await sign_in.close()

    @staticmethod
    def logout(name: str, spec: Mapping[str, Any], home: Path) -> bool:
        """Forget the server's tokens (its client registration stays);
        ``False`` when there were none."""
        store = store_for(_server(name, spec), home=home)
        with store.locked():
            data = store.load()
            if not data.get("tokens"):
                return False
            store.save({k: v for k, v in data.items() if k != "tokens"})
        return True
