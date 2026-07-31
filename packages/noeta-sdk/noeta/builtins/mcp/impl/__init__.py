"""``mcp`` built-in — the MCP (Model Context Protocol) connector (impl).

Exposes tools from operator-allowlisted local stdio / remote HTTP MCP
servers as ordinary Noeta ``Tool``s, governed by
the ``PermissionGuard`` / approval and recorded in the EventLog.

The connector
*vocabulary* (``MCP_PREFIX`` / ``McpConfigError`` / ``McpError`` /
``HttpPostFn`` / the server specs) lives in :mod:`noeta.runtime.mcp` — the
kernel builder's reserved-prefix check and the ``noeta.sdk`` public surface
speak it statically; this package re-exports those names so the connector's
one import surface stays whole. The SDK host reaches ``build_mcp_tools`` /
``mcp_provenance_from_specs`` through the loader's dynamic-import doorway
(:mod:`noeta.client.parts`).
"""

from __future__ import annotations

from noeta.builtins.mcp.impl._client import (
    DEFAULT_MCP_TIMEOUT_S,
    McpStdioClient,
)
from noeta.builtins.mcp.impl._http_client import (
    DEFAULT_MCP_HTTP_TIMEOUT_S,
    McpHttpClient,
)
from noeta.runtime.mcp import HttpPostFn, McpError
from noeta.builtins.mcp.impl.prompts import (
    MCP_PROMPT_ORIGIN_PREFIX,
    discover_prompts,
    expand_prompt,
    flatten_prompt_messages,
    make_mcp_prompt_name,
)
from noeta.builtins.mcp.impl.resources import (
    MCP_RESOURCE_ORIGIN_PREFIX,
    discover_resources,
    flatten_resource_contents,
    make_mcp_resource_ref,
    read_resource,
)
from noeta.builtins.mcp.impl.tool import (
    McpServerSkip,
    McpTool,
    McpToolSpec,
    build_mcp_tools,
    is_mcp_tool_name,
    make_mcp_tool_name,
    mcp_provenance_from_specs,
    parse_mcp_tool_specs,
)
from noeta.runtime.mcp import (
    MCP_PREFIX,
    McpAnyServerSpec,
    McpConfigError,
    McpHttpServerSpec,
    McpServerSpec,
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
