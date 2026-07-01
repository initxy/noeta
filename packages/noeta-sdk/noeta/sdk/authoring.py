"""noeta.sdk authoring API — user-side helpers for defining custom tools and
in-process MCP servers.

These are *authoring* helpers: they belong on the user-facing SDK surface even
though the objects they produce are runtime types. The ``@tool`` decorator is
re-exported verbatim from ``noeta.tools.decorator`` (it moved into noeta-runtime
in T1) so a decorated tool is the **same** ``DecoratedTool`` class the runtime
registers and identifies — re-exporting keeps one object identity while giving
users a single import home: ``from noeta.sdk import tool``.

``create_sdk_mcp_server`` mirrors claude-agent-sdk: it bundles a set of
``@tool`` functions into an in-process ("sdk" transport) MCP server that an
agent can use without spawning a subprocess or a network round-trip. The
resulting :class:`SdkMcpServer` value object is consumed by
``Options.mcp_servers`` (T3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from noeta.tools.decorator import DecoratedTool, tool


__all__ = [
    "DecoratedTool",
    "SdkMcpServer",
    "create_sdk_mcp_server",
    "tool",
]


@dataclass(frozen=True)
class SdkMcpServer:
    """An in-process (``"sdk"`` transport) MCP server definition.

    Produced by :func:`create_sdk_mcp_server`; consumed by
    ``Options.mcp_servers`` (T3) to expose a bundle of ``@tool`` functions to
    an agent in the same process — the noeta analogue of claude-agent-sdk's
    ``create_sdk_mcp_server``. Frozen + tuple-valued so it is hashable and
    carries no mutable state (consistent with the other recipe-layer types).

    Parameters
    ----------
    name:
        Server name. Becomes the MCP server alias the agent's tools are
        namespaced under.
    version:
        Server version string (informational; defaults to ``"1.0.0"``).
    tools:
        The ``@tool``-decorated tools this server exposes.
    """

    name: str
    version: str = "1.0.0"
    tools: tuple[DecoratedTool, ...] = ()


def create_sdk_mcp_server(
    name: str,
    version: str = "1.0.0",
    tools: Iterable[DecoratedTool] = (),
) -> SdkMcpServer:
    """Bundle ``@tool`` functions into an in-process MCP server definition.

    The noeta analogue of claude-agent-sdk's ``create_sdk_mcp_server``: instead
    of pointing at a subprocess (``stdio``) or a URL (``http``), the tools run
    in the host process. Pass the returned :class:`SdkMcpServer` into
    ``Options.mcp_servers`` (T3) to make its tools available to an agent.

    Parameters
    ----------
    name:
        Non-empty server name.
    version:
        Server version string. Defaults to ``"1.0.0"``.
    tools:
        Iterable of :class:`DecoratedTool` (i.e. ``@tool``-decorated
        functions). Each entry must be a ``DecoratedTool``; anything else
        raises ``TypeError`` so a misuse fails loudly at authoring time
        rather than producing a server with a non-runnable tool.

    Returns
    -------
    SdkMcpServer
        A frozen value object describing the in-process server.
    """
    if not name or not name.strip():
        raise ValueError("create_sdk_mcp_server: `name` must be non-empty")
    resolved: list[DecoratedTool] = []
    for entry in tools:
        if not isinstance(entry, DecoratedTool):
            raise TypeError(
                "create_sdk_mcp_server: every tool must be a DecoratedTool "
                "(a @tool-decorated function); got "
                f"{type(entry).__name__}"
            )
        resolved.append(entry)
    return SdkMcpServer(name=name, version=version, tools=tuple(resolved))
