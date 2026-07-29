"""``mcp`` — thin declaration for the MCP connector.

MCP tools are live, host-wired connectors (the host resolves per-turn server
aliases through its ``mcp_server_resolver`` seam and passes the discovered
tools as the builder's ``mcp_tools_override``), so the manifest carries no
contributions — a connector tool is never agent identity. The activation name
``mcp`` flips the ``Capabilities.mcp`` flag; the implementation lives in this
plugin's ``impl`` package (``noeta.builtins.mcp.impl`` — microkernel M3),
reached by the SDK host through the loader's dynamic-import doorway
(:func:`noeta.client.parts.mcp_impl`); the connector *vocabulary* (server
specs, errors, the reserved ``mcp__`` prefix) is kernel-side in
:mod:`noeta.runtime.mcp`.
"""

from __future__ import annotations

from noeta.client.plugin_manifest import PluginManifest


MANIFEST = PluginManifest(name="mcp", requires_noeta=">=0.4")
