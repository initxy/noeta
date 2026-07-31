"""In-process MCP server value object + factory (authoring surface).

:class:`SdkMcpServer` / :func:`create_sdk_mcp_server` are *authoring*
helpers whose public import home is ``noeta.sdk`` (re-exported through
:mod:`noeta.sdk.authoring`, the same discipline as ``@tool``). They live
here — in ``noeta.client``, below ``noeta.sdk`` in the import bands — so
:mod:`noeta.client.options` can name the real type on
``Options.mcp_servers`` instead of duck-typing ``Any`` entries by their
``.tools`` attribute (the pre-cleanup workaround for the would-be upward
import; see the sdk-layer-cleanup spec, D2).

``create_sdk_mcp_server`` mirrors claude-agent-sdk: it bundles a set of
``@tool`` functions into an in-process ("sdk" transport) MCP server that an
agent can use without spawning a subprocess or a network round-trip. The
resulting :class:`SdkMcpServer` value object is consumed by
``Options.mcp_servers``.
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

    Produced by :func:`create_sdk_mcp_server`; consumed by
    ``Options.mcp_servers`` to expose a bundle of ``@tool`` functions to
    an agent in the same process — the noeta analogue of claude-agent-sdk's
    ``create_sdk_mcp_server``. Frozen + tuple-valued so it is hashable and
    carries no mutable state (consistent with the other recipe-layer types).

    Parameters
    ----------
    name:
        Server name — a grouping label for the bundle. It does **not**
        namespace the tools: an in-process server's tools keep their bare
        ``@tool`` names (the model sees ``fetch_weather``, not
        ``mcp__weather-tools__fetch_weather``). The ``mcp__{alias}__{tool}``
        prefix applies only to REMOTE servers connected per turn through
        ``HostConfig.mcp_server_resolver``, where third-party name collisions
        are the concern. Choose bare names that will not collide with a
        built-in tool.
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
    ``Options.mcp_servers`` to make its tools available to an agent.

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
