"""Local stdio MCP (Model Context Protocol) external tool registration.

Exposes tools from operator-allowlisted local stdio MCP
servers as ordinary Noeta ``Tool``s, governed by
the existing ``PermissionGuard`` / approval and recorded in the EventLog.
"""

from __future__ import annotations

from noeta.tools.mcp._client import (
    DEFAULT_MCP_TIMEOUT_S,
    McpError,
    McpStdioClient,
)
from noeta.tools.mcp._http_client import (
    DEFAULT_MCP_HTTP_TIMEOUT_S,
    HttpPostFn,
    McpHttpClient,
)
from noeta.tools.mcp.prompts import (
    MCP_PROMPT_ORIGIN_PREFIX,
    discover_prompts,
    expand_prompt,
    flatten_prompt_messages,
    make_mcp_prompt_name,
)
from noeta.tools.mcp.resources import (
    MCP_RESOURCE_ORIGIN_PREFIX,
    discover_resources,
    flatten_resource_contents,
    make_mcp_resource_ref,
    read_resource,
)
from noeta.tools.mcp.tool import (
    MCP_PREFIX,
    McpAnyServerSpec,
    McpConfigError,
    McpHttpServerSpec,
    McpServerSkip,
    McpServerSpec,
    McpTool,
    McpToolSpec,
    build_mcp_tools,
    is_mcp_tool_name,
    make_mcp_tool_name,
    mcp_provenance_from_specs,
    parse_mcp_tool_specs,
)


__all__ = [
    "DEFAULT_MCP_HTTP_TIMEOUT_S",
    "DEFAULT_MCP_TIMEOUT_S",
    "HttpPostFn",
    "MCP_PREFIX",
    "MCP_PROMPT_ORIGIN_PREFIX",
    "MCP_RESOURCE_ORIGIN_PREFIX",
    "McpAnyServerSpec",
    "McpConfigError",
    "McpError",
    "McpHttpClient",
    "McpHttpServerSpec",
    "McpServerSkip",
    "McpServerSpec",
    "McpStdioClient",
    "McpTool",
    "McpToolSpec",
    "build_mcp_tools",
    "discover_prompts",
    "discover_resources",
    "expand_prompt",
    "flatten_prompt_messages",
    "flatten_resource_contents",
    "is_mcp_tool_name",
    "make_mcp_prompt_name",
    "make_mcp_resource_ref",
    "make_mcp_tool_name",
    "mcp_provenance_from_specs",
    "parse_mcp_tool_specs",
    "read_resource",
]
