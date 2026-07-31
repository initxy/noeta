"""User-side helpers for defining custom tools and in-process MCP servers.

These belong on the user-facing SDK surface but are defined deeper in the import
graph — ``@tool`` in ``noeta.tools.decorator`` so a decorated tool is the *same*
``DecoratedTool`` class the runtime registers and identifies, and ``SdkMcpServer``
in ``noeta.client.mcp_server`` so ``Options.mcp_servers`` carries the real type
rather than ``Any``. Re-exporting verbatim keeps one object identity per helper
while giving users a single import home.
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
