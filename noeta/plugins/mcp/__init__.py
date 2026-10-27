"""MCP: tools from Model Context Protocol servers (stdio and Streamable HTTP).

``Mcp`` is the extension; ``McpServer`` is what a host configures; prompts and
resources are reached through the helpers in :mod:`noeta.plugins.mcp.prompts`.
"""

from noeta.plugins.mcp.config import (
    MCP_PREFIX,
    McpConfigError,
    McpError,
    McpServer,
    make_tool_name,
)
from noeta.plugins.mcp.extension import Mcp, ServersFn
from noeta.plugins.mcp.pool import McpPool, default_pool
from noeta.plugins.mcp.prompts import (
    McpPrompt,
    McpResource,
    expand_prompt,
    list_prompts,
    list_resources,
    read_resource,
)

__all__ = [
    "MCP_PREFIX",
    "Mcp",
    "McpConfigError",
    "McpError",
    "McpPool",
    "McpPrompt",
    "McpResource",
    "McpServer",
    "ServersFn",
    "default_pool",
    "expand_prompt",
    "list_prompts",
    "list_resources",
    "make_tool_name",
    "read_resource",
]
