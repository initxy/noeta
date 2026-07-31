"""noeta.sdk authoring API — user-side helpers for defining custom tools and
in-process MCP servers.

These are *authoring* helpers: they belong on the user-facing SDK surface even
though the objects they produce are defined lower in the import bands. The
``@tool`` decorator is re-exported verbatim from ``noeta.tools.decorator`` (it
moved into noeta-runtime in T1) so a decorated tool is the **same**
``DecoratedTool`` class the runtime registers and identifies. ``SdkMcpServer``
/ ``create_sdk_mcp_server`` are re-exported from
``noeta.client.mcp_server`` for the same reason (sdk-layer-cleanup spec, D2):
defining them below ``noeta.client`` lets ``Options.mcp_servers`` carry the
real type instead of ``Any``. Re-exporting keeps one object identity per
helper while giving users a single import home: ``from noeta.sdk import tool``.
"""

from __future__ import annotations

from noeta.client.mcp_server import SdkMcpServer, create_sdk_mcp_server
from noeta.tools.decorator import DecoratedTool, tool


__all__ = [
    "DecoratedTool",
    "SdkMcpServer",
    "create_sdk_mcp_server",
    "tool",
]
