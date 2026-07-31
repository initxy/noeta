"""In-process MCP server value object and its authoring factory.

The public import home of both names is ``noeta.sdk``, but the type is defined
here in ``noeta.client`` — below ``noeta.sdk`` in the import bands — so
:mod:`noeta.client.options` can name it on ``Options.mcp_servers`` instead of
duck-typing ``Any`` entries by their ``.tools`` attribute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from noeta.tools.decorator import DecoratedTool


__all__ = [
    "SdkMcpServer",
    "create_sdk_mcp_server",
]


@dataclass(frozen=True)
class SdkMcpServer:
    """An in-process (``"sdk"`` transport) MCP server definition.

    ``name`` is a grouping label for the bundle; it does **not** namespace the
    tools. An in-process server's tools keep their bare ``@tool`` names (the
    model sees ``fetch_weather``, not ``mcp__weather-tools__fetch_weather``) —
    the ``mcp__{alias}__{tool}`` prefix applies only to REMOTE servers connected
    per turn through ``HostConfig.mcp_server_resolver``, where third-party name
    collisions are the concern. Choose bare names that will not collide with a
    built-in tool.
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

    The tools run in the host process rather than behind a subprocess
    (``stdio``) or a URL (``http``). An entry that is not a
    :class:`DecoratedTool` raises ``TypeError``, so a misuse fails loudly at
    authoring time instead of producing a server with a non-runnable tool.
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
