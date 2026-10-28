"""Where a remote MCP server signs people in (:mod:`noeta.plugins.mcp.oauth`,
step 1): its ``WWW-Authenticate`` challenge, its protected resource metadata
(RFC 9728) and its authorization server's metadata (RFC 8414, else OpenID
discovery)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from noeta.plugins.mcp.config import McpError, McpServer, OAuthSettings

#: Seconds one metadata, registration or token request may take.
HTTP_TIMEOUT_S = 30.0


# ---------------------------------------------------------------- challenge


def parse_challenge(header: str) -> dict[str, str]:
    """The parameters of a ``Bearer`` ``WWW-Authenticate`` challenge."""
    out: dict[str, str] = {}
    text = header.strip()
    if text.lower().startswith("bearer"):
        text = text[len("bearer"):]
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in " ,":
            i += 1
        eq = text.find("=", i)
        if eq < 0:
            break
        key = text[i:eq].strip().lower()
        i = eq + 1
        if i < len(text) and text[i] == '"':
            end = i + 1
            value = []
            while end < len(text) and text[end] != '"':
                if text[end] == "\\" and end + 1 < len(text):
                    end += 1
                value.append(text[end])
                end += 1
            out[key] = "".join(value)
            i = end + 1
        else:
            end = text.find(",", i)
            end = len(text) if end < 0 else end
            out[key] = text[i:end].strip()
            i = end
    return out


# ---------------------------------------------------------------- discovery


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _path(url: str) -> str:
    return urllib.parse.urlsplit(url).path.rstrip("/")


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        resp = await http.get(url, headers={"Accept": "application/json",
                                            "MCP-Protocol-Version": "2025-11-25"},
                              timeout=HTTP_TIMEOUT_S)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _resource_metadata(http: httpx.AsyncClient, server_url: str,
                             hint: str) -> dict[str, Any] | None:
    urls = [hint] if hint else []
    base = _origin(server_url) + "/.well-known/oauth-protected-resource"
    if _path(server_url):
        urls.append(base + _path(server_url))
    urls.append(base)
    for url in urls:
        found = await _get_json(http, url)
        if found is not None and found.get("authorization_servers"):
            return found
    return None


async def _server_metadata(http: httpx.AsyncClient, issuer: str) -> dict[str, Any] | None:
    origin, path = _origin(issuer), _path(issuer)
    if path:
        urls = [f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{origin}{path}/.well-known/openid-configuration"]
    else:
        urls = [f"{origin}/.well-known/oauth-authorization-server",
                f"{origin}/.well-known/openid-configuration"]
    for url in urls:
        found = await _get_json(http, url)
        if found is not None and found.get("authorization_endpoint") \
                and found.get("token_endpoint"):
            return found
    return None


@dataclass
class Endpoints:
    authorization: str
    token: str
    registration: str
    auth_methods: list[str]
    #: The resource indicator (RFC 8707), when the server published one.
    resource: str
    scopes: str


async def discover(http: httpx.AsyncClient, server: McpServer,
                   challenge: str = "") -> Endpoints:
    """The endpoints a sign-in with ``server`` uses."""
    settings = server.oauth or OAuthSettings()
    params = parse_challenge(challenge)
    prm = await _resource_metadata(http, server.url, params.get("resource_metadata", ""))
    meta: dict[str, Any] | None
    if settings.auth_server_metadata_url:
        if not settings.auth_server_metadata_url.startswith("https://"):
            raise McpError("authServerMetadataUrl must use https://")
        meta = await _get_json(http, settings.auth_server_metadata_url)
    else:
        issuer = str((prm or {}).get("authorization_servers", [""])[0] or "") \
            or _origin(server.url)
        meta = await _server_metadata(http, issuer)
        if meta is None and prm is None:
            # A server from before resource metadata: its own endpoints.
            origin = _origin(server.url)
            meta = {"authorization_endpoint": f"{origin}/authorize",
                    "token_endpoint": f"{origin}/token",
                    "registration_endpoint": f"{origin}/register"}
    if meta is None:
        raise McpError(f"MCP server {server.alias!r}: no OAuth authorization server metadata "
                       "found")
    scopes = settings.scopes or params.get("scope", "") or " ".join(
        str(s) for s in (prm or {}).get("scopes_supported") or [])
    return Endpoints(
        authorization=str(meta["authorization_endpoint"]), token=str(meta["token_endpoint"]),
        registration=str(meta.get("registration_endpoint") or ""),
        auth_methods=[str(m) for m in meta.get("token_endpoint_auth_methods_supported") or []],
        resource=str((prm or {}).get("resource") or ""), scopes=scopes)
