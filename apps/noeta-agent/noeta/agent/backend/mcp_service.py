"""mcp_service — the MCP connector management routes (ancillary service, T6).

MCP management is an ancillary resource
service, physically separate from the core task protocol (``/stream`` +
``/tasks/*``). It is the CRUD + discovery face over the host's MCP connector
config store (``noeta.agent.host.mcp_registry.McpServerRegistry``); the live
per-turn connection is a separate concern — the store's ``resolve_spec`` is
wired into the engine as the noeta.sdk ``HostConfig.mcp_server_resolver`` (the SDK
connects enabled aliases each turn), while these endpoints only configure and
introspect the connectors.

Endpoints (all under ``/mcp``):

* ``GET    /mcp/servers``                  — list connectors (credential-scrubbed)
* ``POST   /mcp/servers``                  — create/replace an http|stdio connector
* ``PUT    /mcp/servers/{alias}``          — edit (merge; omitted fields kept)
* ``DELETE /mcp/servers/{alias}``          — remove a connector
* ``GET    /mcp/servers/{alias}/tools``    — the connector's full tool menu
* ``PUT    /mcp/servers/{alias}/tools``    — set the enabled tool subset
* ``GET    /mcp/servers/{alias}/prompts``  — the connector's prompts (slash cmds)
* ``GET    /mcp/servers/{alias}/resources``— the connector's static resources

Credentials (header values / env values) are stored host-side only and never
echoed back (the registry's ``as_public_dict`` scrubs them). A connect/handshake
failure on the discovery endpoints maps to ``502``; a bad config to ``400``;
an absent registry to ``503``.
"""

from __future__ import annotations

from typing import Any, Optional

from noeta.sdk import McpConfigError, McpError

from noeta.agent.backend.app import BackendHandler, Router


def _registry_or_503(handler: BackendHandler) -> Optional[Any]:
    reg = handler.mcp_registry
    if reg is None:
        handler.send_json(
            {"error": "MCP server registry is not configured"}, status=503
        )
    return reg


def _read_tool_subset(raw: Any) -> tuple[Optional[list[str]], Optional[str]]:
    """Validate the optional ``tools`` subset → ``(subset, error)``.

    Absent / ``null`` ⇒ keep all (``None``); a list of non-empty strings ⇒ that
    subset; anything else ⇒ an error message.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(t, str) and t for t in raw):
        return None, "'tools' must be a list of non-empty strings when present"
    return list(raw), None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _handle_list(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    handler.send_json({"servers": [e.as_public_dict() for e in reg.list_all()]})


def _handle_create(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    body = handler.read_json_body()
    alias = body.get("alias")
    server_type = body.get("type", "http")
    if not isinstance(alias, str) or not alias:
        handler.send_json({"error": "'alias' is required"}, status=400)
        return
    if server_type not in ("http", "stdio"):
        handler.send_json({"error": "'type' must be 'http' or 'stdio'"}, status=400)
        return
    tools, terr = _read_tool_subset(body.get("tools"))
    if terr is not None:
        handler.send_json({"error": terr}, status=400)
        return
    try:
        if server_type == "http":
            url = body.get("url")
            headers = body.get("headers", {})
            if not isinstance(url, str) or not url:
                handler.send_json({"error": "'url' is required for http"}, status=400)
                return
            if not isinstance(headers, dict):
                handler.send_json({"error": "'headers' must be an object"}, status=400)
                return
            entry = reg.upsert_http(
                alias=alias, url=url, headers=dict(headers), tools=tools
            )
        else:
            command = body.get("command")
            args = body.get("args", [])
            env = body.get("env", {})
            if not isinstance(command, str) or not command:
                handler.send_json(
                    {"error": "'command' is required for stdio"}, status=400
                )
                return
            if not isinstance(args, list) or not isinstance(env, dict):
                handler.send_json(
                    {"error": "'args' must be a list and 'env' an object"}, status=400
                )
                return
            entry = reg.upsert_stdio(
                alias=alias,
                command=command,
                args=list(args),
                env=dict(env),
                tools=tools,
            )
    except (McpConfigError, ValueError) as exc:
        handler.send_json({"error": str(exc)}, status=400)
        return
    handler.send_json(entry.as_public_dict(), status=201)


def _handle_update(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    body = handler.read_json_body()
    tools_present = "tools" in body
    tools, terr = _read_tool_subset(body.get("tools"))
    if terr is not None:
        handler.send_json({"error": terr}, status=400)
        return
    try:
        entry = reg.update_merge(
            params["alias"],
            url=body.get("url"),
            headers=body.get("headers"),
            command=body.get("command"),
            args=body.get("args"),
            env=body.get("env"),
            tools=tools if tools_present else None,
            clear_tools=tools_present and tools is None,
        )
    except (McpConfigError, ValueError) as exc:
        handler.send_json({"error": str(exc)}, status=400)
        return
    if entry is None:
        handler.send_json({"error": "unknown alias"}, status=404)
        return
    handler.send_json(entry.as_public_dict())


def _handle_delete(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    if reg.delete(params["alias"]):
        handler.send_json({"deleted": params["alias"]})
    else:
        handler.send_json({"error": "unknown alias"}, status=404)


# ---------------------------------------------------------------------------
# Discovery (connect → list → shut down)
# ---------------------------------------------------------------------------


def _discover(handler: BackendHandler, key: str, fn: Any, alias: str) -> None:
    try:
        items = fn(alias)
    except (McpConfigError, McpError) as exc:
        handler.send_json({"error": str(exc)}, status=502)
        return
    if items is None:
        handler.send_json({"error": "unknown alias"}, status=404)
        return
    handler.send_json({key: items})


def _handle_tools(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    _discover(handler, "tools", reg.discover_tools, params["alias"])


def _handle_set_tools(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    body = handler.read_json_body()
    tools, terr = _read_tool_subset(body.get("tools"))
    if terr is not None:
        handler.send_json({"error": terr}, status=400)
        return
    entry = reg.set_tools(params["alias"], tools)
    if entry is None:
        handler.send_json({"error": "unknown alias"}, status=404)
        return
    handler.send_json(entry.as_public_dict())


def _handle_prompts(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    _discover(handler, "prompts", reg.discover_prompts, params["alias"])


def _handle_resources(handler: BackendHandler, params: dict[str, str]) -> None:
    reg = _registry_or_503(handler)
    if reg is None:
        return
    _discover(handler, "resources", reg.discover_resources, params["alias"])


def register_mcp_routes(router: Router) -> None:
    """Register the MCP connector management routes onto ``router`` (T6)."""
    # Sub-resources before the bare ``{alias}`` so they match first.
    router.add("GET", "/mcp/servers/{alias}/tools", _handle_tools)
    router.add("PUT", "/mcp/servers/{alias}/tools", _handle_set_tools)
    router.add("GET", "/mcp/servers/{alias}/prompts", _handle_prompts)
    router.add("GET", "/mcp/servers/{alias}/resources", _handle_resources)
    router.add("GET", "/mcp/servers", _handle_list)
    router.add("POST", "/mcp/servers", _handle_create)
    router.add("PUT", "/mcp/servers/{alias}", _handle_update)
    router.add("DELETE", "/mcp/servers/{alias}", _handle_delete)
