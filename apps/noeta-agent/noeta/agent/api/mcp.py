"""MCP connector API: per-space connector CRUD + discovery menus.

The back-port of the retired app's MCP connector management, re-scoped from a
global registry to per-space configuration (D9 item 1). Endpoints (all under
``/spaces/{space_id}/mcp``):

* ``GET    /mcp/servers``                    — list connectors (credential-scrubbed)
* ``POST   /mcp/servers``                    — create/replace an http|stdio connector
* ``PUT    /mcp/servers/{alias}``            — edit (merge; omitted fields kept)
* ``PATCH  /mcp/servers/{alias}``            — enable / disable
* ``DELETE /mcp/servers/{alias}``            — remove a connector
* ``GET    /mcp/servers/{alias}/tools``      — the connector's full tool menu
* ``PUT    /mcp/servers/{alias}/tools``      — set the enabled tool subset
* ``GET    /mcp/servers/{alias}/prompts``    — the connector's prompts
* ``GET    /mcp/servers/{alias}/resources``  — the connector's static resources

Permission model (following the membership-check pattern in knowledge.py):
space members can read; only the space owner can manage. Non-members -> 404
(hiding existence).

Credentials (header values / env values) are stored server-side only and
never echoed back (the store's ``as_public_dict`` scrubs them to names). A
connect/handshake failure on the discovery endpoints maps to ``502``; a bad
config to ``400`` — the retired app's error mapping. Discovery is HTTP-only:
a stdio connector's menus map to ``400`` (the server does not spawn
operator-configured subprocesses from a management GET).
"""
from __future__ import annotations

from functools import partial
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from noeta.sdk import McpConfigError, McpError

from noeta.agent.auth.deps import CurrentUser, get_current_user
from noeta.agent.services import mcp_discovery
from noeta.agent.store.mcp import McpConnector, McpConnectorStore, VALID_TYPES
from noeta.agent.store.spaces import ROLE_OWNER, SpaceStore

router = APIRouter(prefix="/spaces/{space_id}/mcp", tags=["mcp"])


def _mcp_store(request: Request) -> McpConnectorStore:
    return request.app.state.mcp_store


def _space_store(request: Request) -> SpaceStore:
    return request.app.state.space_store


def _membership_or_404(
    request: Request, space_id: str, user: CurrentUser
) -> tuple[dict, str]:
    """Space missing or not a member -> 404 (hiding existence)."""
    store = _space_store(request)
    space = store.get_space(space_id)
    role = store.get_member_role(space_id, user.username) if space else None
    if space is None or role is None:
        raise HTTPException(status_code=404, detail="space not found")
    return space, role


def _require_owner(role: str) -> None:
    if role != ROLE_OWNER:
        raise HTTPException(status_code=403, detail="space owner permission required")


def _connector_or_404(
    request: Request, space_id: str, alias: str
) -> McpConnector:
    connector = _mcp_store(request).get(space_id, alias)
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return connector


# ---------------------------------------------------------------- models


class CreateConnectorBody(BaseModel):
    alias: str = Field(min_length=1, max_length=64)
    type: str = "http"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tools: Optional[list[str]] = None
    enabled: bool = True


class UpdateConnectorBody(BaseModel):
    """Merge edit: an omitted field keeps its current value, so editing a url
    never requires re-pasting the credential headers. ``tools`` present with
    ``null`` explicitly clears the subset (all advertised tools)."""

    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    tools: Optional[list[str]] = None
    enabled: Optional[bool] = None


class ToggleConnectorBody(BaseModel):
    enabled: bool


class SetToolsBody(BaseModel):
    """``tools: null`` clears the subset (all advertised tools enabled); a
    list restricts to those raw names."""

    tools: Optional[list[str]] = None


def _validate_tool_subset(tools: Optional[list[str]]) -> None:
    if tools is not None and not all(isinstance(t, str) and t for t in tools):
        raise HTTPException(
            status_code=400,
            detail="'tools' must be a list of non-empty strings when present",
        )


# ----------------------------------------------------------------- CRUD


@router.get("/servers")
async def list_connectors(
    space_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    connectors = _mcp_store(request).list_for_space(space_id)
    return {"servers": [c.as_public_dict() for c in connectors]}


@router.post("/servers", status_code=201)
async def create_connector(
    space_id: str,
    body: CreateConnectorBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    if body.type not in VALID_TYPES:
        raise HTTPException(
            status_code=400, detail="'type' must be 'http' or 'stdio'"
        )
    if body.type == "http" and not body.url:
        raise HTTPException(status_code=400, detail="'url' is required for http")
    if body.type == "stdio" and not body.command:
        raise HTTPException(
            status_code=400, detail="'command' is required for stdio"
        )
    _validate_tool_subset(body.tools)
    try:
        connector = _mcp_store(request).upsert(
            space_id,
            body.alias,
            connector_type=body.type,
            url=body.url,
            headers=body.headers,
            command=body.command,
            args=body.args,
            env=body.env,
            tools=body.tools,
            enabled=body.enabled,
            created_by=user.username,
        )
    except (McpConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"server": connector.as_public_dict()}


@router.put("/servers/{alias}")
async def update_connector(
    space_id: str,
    alias: str,
    body: UpdateConnectorBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    tools_present = "tools" in body.model_fields_set
    _validate_tool_subset(body.tools)
    try:
        connector = _mcp_store(request).update_merge(
            space_id,
            alias,
            url=body.url,
            headers=body.headers,
            command=body.command,
            args=body.args,
            env=body.env,
            tools=body.tools if tools_present else None,
            clear_tools=tools_present and body.tools is None,
            enabled=body.enabled,
        )
    except (McpConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return {"server": connector.as_public_dict()}


@router.patch("/servers/{alias}")
async def toggle_connector(
    space_id: str,
    alias: str,
    body: ToggleConnectorBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Enable / disable a connector (owner only). Once disabled, new turns no
    longer connect it; in-flight turns are unaffected."""
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    connector = _mcp_store(request).set_enabled(space_id, alias, body.enabled)
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return {"server": connector.as_public_dict()}


@router.delete("/servers/{alias}")
async def delete_connector(
    space_id: str,
    alias: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    if not _mcp_store(request).delete(space_id, alias):
        raise HTTPException(status_code=404, detail="connector not found")
    return {"ok": True}


# ------------------------------------------------------------- tool subset


@router.put("/servers/{alias}/tools")
async def set_tool_subset(
    space_id: str,
    alias: str,
    body: SetToolsBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _space, role = _membership_or_404(request, space_id, user)
    _require_owner(role)
    _validate_tool_subset(body.tools)
    connector = _mcp_store(request).set_tools(space_id, alias, body.tools)
    if connector is None:
        raise HTTPException(status_code=404, detail="connector not found")
    return {"server": connector.as_public_dict()}


# ---------------------------------------------- discovery (connect + list)


async def _discover(
    request: Request, space_id: str, alias: str, key: str, fn: Any
) -> dict:
    """Shared discovery flow: resolve the connector, connect over HTTP, map
    faults (bad config -> 400, connect/handshake failure -> 502)."""
    connector = _connector_or_404(request, space_id, alias)
    if connector.type != "http":
        raise HTTPException(
            status_code=400,
            detail="discovery is only supported for http connectors",
        )
    try:
        # The connect is blocking network I/O; keep the event loop free.
        items = await anyio.to_thread.run_sync(partial(fn, connector))
    except McpConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except McpError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {key: items}


@router.get("/servers/{alias}/tools")
async def list_tool_menu(
    space_id: str,
    alias: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """The connector's FULL advertised tool menu (ignores the stored subset —
    the menu must show every candidate, not just the already-ticked ones)."""
    _membership_or_404(request, space_id, user)
    return await _discover(
        request,
        space_id,
        alias,
        "tools",
        lambda c: mcp_discovery.discover_tools(c.url, c.headers),
    )


@router.get("/servers/{alias}/prompts")
async def list_prompt_menu(
    space_id: str,
    alias: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return await _discover(
        request,
        space_id,
        alias,
        "prompts",
        lambda c: mcp_discovery.discover_prompts(c.alias, c.url, c.headers),
    )


@router.get("/servers/{alias}/resources")
async def list_resource_menu(
    space_id: str,
    alias: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    _membership_or_404(request, space_id, user)
    return await _discover(
        request,
        space_id,
        alias,
        "resources",
        lambda c: mcp_discovery.discover_resources(c.alias, c.url, c.headers),
    )
