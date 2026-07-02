"""MCP resources → unified ``@`` content channel.

A remote / local MCP server can advertise **resources** (``resources/list``) —
named blobs of *reference material* the user attaches into a turn.
Makes them the MCP half of the unified ``@`` mention: ``@<alias>:<uri>`` references
an MCP resource, ``@<path>`` references a workspace file,
and BOTH ride the SAME content channel — read
at send time, snapshotted into an ordinary recorded message tagged
``origin="system"`` (resume reads the snapshot, never re-reads).

The Noeta commitments held verbatim from D4 / D10:

* **request-response subset only** — one ``resources/list`` / ``resources/read``
  POST, one JSON-RPC response; never a server-push stream;
* **v1 static clip-list only** — what ``resources/list`` enumerates; resource
  *templates* (parameterised URIs) are out of scope (they need extra fill-in);
* **user-driven only** — there is NO tool / channel that lets the model pull a
  resource; the model takes information through tools (D10's resource=read-material
  / tool=do-action boundary). These two helpers are reached ONLY from the host's
  ``@``-mention handling, never from the tool set.

The two entrypoints connect a single server spec (HTTP or stdio), do one
request-response round-trip, and shut the client down. Credentials live in the
spec and ride the wire only; nothing here records them.

* :func:`discover_resources` — ``resources/list`` → ``[{uri, name, description,
  mime_type, noeta_ref}]`` for the unified ``@`` selector (``noeta_ref`` is the
  ``<alias>:<uri>`` mention token). A server that does not support resources
  returns ``[]`` (the fault is swallowed — resources are optional).
* :func:`read_resource` — ``resources/read`` → the flattened text snapshot the
  host records as an ``origin="system"`` message. Faults propagate (``McpError``
  / ``McpConfigError``) so the HTTP layer maps a bad alias / unreachable server
  to a typed error.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.tools.mcp._client import McpError, SpawnFn
from noeta.tools.mcp._http_client import HttpPostFn
from noeta.tools.mcp.tool import McpAnyServerSpec, _connect_client, cap_injected


__all__ = [
    "MCP_RESOURCE_ORIGIN_PREFIX",
    "discover_resources",
    "flatten_resource_contents",
    "make_mcp_resource_ref",
    "read_resource",
]


#: A human-readable provenance prefix the host prepends to a snapshotted resource
#: so the conversation transcript shows "this came from an MCP resource" (the
#: structural origin is ``Message.origin="system"``; this is the visible label,
#: mirroring ``mcp-prompt`` / memory recall attribution).
MCP_RESOURCE_ORIGIN_PREFIX = "mcp-resource"


def make_mcp_resource_ref(alias: str, uri: str) -> str:
    """The unified-``@`` mention token for one MCP resource: ``<alias>:<uri>``.

    The front-end ``@`` selector lists this verbatim and the request body carries
    it back (``{alias, uri}``); the host resolves the alias to its host-side spec
    (no url / token rides the request, D3) and reads the URI at send time."""
    return f"{alias}:{uri}"


def flatten_resource_contents(result: dict[str, Any]) -> str:
    """Flatten a ``resources/read`` result's contents into one snapshot string.

    An MCP ``resources/read`` returns ``{contents: [{uri, mimeType?, text?,
    blob?}]}``. We keep the **text** of every text content, in order, joined by
    blank lines (binary ``blob`` contents — images / archives — are out of scope
    for the v1 text snapshot and are skipped). Snapshotting the text (not the URI)
    at send time is what makes resume deterministic: a later edit to the resource
    cannot drift an already-recorded message (D10)."""
    contents = result.get("contents")
    parts: list[str] = []
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return cap_injected("\n\n".join(parts), kind="resource")


def discover_resources(
    spec: McpAnyServerSpec,
    *,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
) -> list[dict[str, Any]]:
    """Connect ``spec``, list its STATIC resources, and shut down.

    Returns ``[{uri, name, description, mime_type, noeta_ref}]`` in advertised
    order (``noeta_ref`` = the ``<alias>:<uri>`` ``@``-mention token the unified
    selector lists). A server that does not implement resources
    (``resources/list`` faults) yields ``[]`` — resources are an optional
    capability, so a missing one is not an error. Connect / handshake faults DO
    propagate (the alias is misconfigured)."""
    client = _connect_client(spec, spawn=spawn, http_post=http_post)
    try:
        client.start()
        try:
            raw_resources = client.list_resources()
        except McpError:
            # Optional capability — a server with no resources surface is fine.
            return []
        out: list[dict[str, Any]] = []
        for r in raw_resources:
            uri = r.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            out.append(
                {
                    "uri": uri,
                    "name": r["name"] if isinstance(r.get("name"), str) else "",
                    "description": (
                        r["description"]
                        if isinstance(r.get("description"), str)
                        else ""
                    ),
                    "mime_type": (
                        r["mimeType"] if isinstance(r.get("mimeType"), str) else ""
                    ),
                    "noeta_ref": make_mcp_resource_ref(spec.alias, uri),
                }
            )
        return out
    finally:
        client.shutdown()


def read_resource(
    spec: McpAnyServerSpec,
    *,
    uri: str,
    spawn: Optional[SpawnFn] = None,
    http_post: Optional[HttpPostFn] = None,
) -> str:
    """Connect ``spec``, ``resources/read`` the URI, flatten its text (D10).

    Returns the plain-text snapshot the host records as that turn's ``@``-mention
    content (an ``origin="system"`` message; resume reads it back, never
    re-reading). Faults propagate so the HTTP layer maps an unreachable server /
    unknown URI to a typed error.
    """
    client = _connect_client(spec, spawn=spawn, http_post=http_post)
    try:
        client.start()
        result = client.read_resource(uri)
        return flatten_resource_contents(result)
    finally:
        client.shutdown()
