"""OAuth 2.1 sign-in for remote MCP servers (``http`` and ``sse``).

A remote server that answers ``401`` wants a bearer token. The way to one,
as the MCP authorization spec and Claude Code do it:

1. **Discovery** (:mod:`.oauth_discovery`). The protected resource metadata (RFC 9728) — at the
   ``resource_metadata`` URL the ``WWW-Authenticate`` challenge names, else
   at the server's ``/.well-known/oauth-protected-resource`` — names the
   authorization server; its metadata (RFC 8414, else OpenID discovery)
   names the endpoints. ``oauth.authServerMetadataUrl`` skips both. A server
   without resource metadata is its own authorization server.
2. **Client.** ``oauth.clientId`` (with a secret stored by ``noeta mcp add
   --client-secret``), else a client registered once (RFC 7591) and kept.
3. **Authorization code with PKCE (S256).** The person opens the
   authorization URL; the browser comes back to
   ``http://localhost:<port>/callback`` on a listener of this process, or
   the person pastes that URL (:meth:`SignIn.submit`) when the browser
   cannot reach it. The port is ``NOETA_MCP_OAUTH_CALLBACK_PORT``, else
   ``oauth.callbackPort`` when free, else a free one. Nobody signing in
   within :data:`SIGN_IN_TIMEOUT_S` ends the attempt.
4. **Tokens** are kept per server (and connection scope) under
   ``$NOETA_HOME/mcp-auth`` (directory 0700, files 0600), renewed with the
   refresh token shortly before they expire or when the server answers 401.

Tokens and secrets never reach the task log.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import json
import os
import random
import secrets
import socket
import tempfile
import time
import urllib.parse
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from noeta.plugins.mcp.config import McpError, McpServer, OAuthSettings
from noeta.plugins.mcp.oauth_discovery import HTTP_TIMEOUT_S as _HTTP_TIMEOUT_S
from noeta.plugins.mcp.oauth_discovery import (
    Endpoints,
    discover,
)
from noeta.plugins.mcp.oauth_discovery import (
    parse_challenge as parse_challenge,
)
from noeta.toolkit.http import DIRECT_LOOPBACK
from noeta.toolkit.paths import noeta_home

__all__ = [
    "AUTH_DIR",
    "CALLBACK_PORT_ENV",
    "SIGN_IN_TIMEOUT_S",
    "OAuth",
    "SignIn",
    "TokenStore",
    "callback_port",
    "parse_challenge",
    "store_for",
]

#: Where tokens live, under noeta's home.
AUTH_DIR = "mcp-auth"
CALLBACK_PORT_ENV = "NOETA_MCP_OAUTH_CALLBACK_PORT"
#: How long a sign-in waits for the browser to come back.
SIGN_IN_TIMEOUT_S = 300.0
#: A token this close to its expiry is renewed before use.
REFRESH_MARGIN_S = 60.0
_FALLBACK_PORT = 3118
_PORT_RANGE = (49152, 65535)
_PORT_TRIES = 100
_DONE_PAGE = (b"<!doctype html><title>Signed in</title>"
              b"<p>Authentication complete. You can close this window.</p>")
_FAILED_PAGE = b"<!doctype html><title>Sign-in failed</title><p>Sign-in failed: "


# ---------------------------------------------------------------- storage


class TokenStore:
    """One server's saved OAuth state (client registration and tokens):
    a JSON file only its owner can read."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _dir(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        return self.path.parent

    def save(self, data: Mapping[str, Any]) -> None:
        """Replace the file at once (a temporary file of its own, renamed)."""
        fd, tmp = tempfile.mkstemp(dir=self._dir(), prefix=f".{self.path.name}.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(data), fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the file's lock (a sibling ``.lock`` file) for a
        read-modify-write, across threads and processes. Without ``fcntl``
        (Windows) there is no lock."""
        try:
            import fcntl
        except ImportError:
            yield
            return
        fd = os.open(self._dir() / f"{self.path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextlib.asynccontextmanager
    async def locked_async(self) -> AsyncIterator[None]:
        """:meth:`locked` held across awaits: the lock is taken in a thread,
        so a wait for another process never stalls the event loop. Nothing
        inside may take :meth:`locked` again (``update``): save instead."""
        cm = self.locked()
        taking = asyncio.ensure_future(asyncio.to_thread(cm.__enter__))
        try:
            await asyncio.shield(taking)
        except asyncio.CancelledError:
            # The thread may still get the lock: let it go once it does.
            taking.add_done_callback(
                lambda t: None if t.cancelled() or t.exception() is not None
                else cm.__exit__(None, None, None))
            raise
        try:
            yield
        finally:
            cm.__exit__(None, None, None)

    def update(self, **fields: Any) -> dict[str, Any]:
        with self.locked():
            data = {**self.load(), **fields}
            self.save(data)
        return data

    async def update_async(self, **fields: Any) -> dict[str, Any]:
        """:meth:`update` in a thread: the lock may be held for a while (a
        refresh elsewhere waits for the network under it), and a wait on
        the event loop would stall the very refresh it waits for."""
        return await asyncio.to_thread(self.update, **fields)

    def clear(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def store_for(server: McpServer, scope: str | None = None,
              home: Path | None = None) -> TokenStore:
    """The token file of ``server`` in connection ``scope``: one per server
    url, so two scopes never share a sign-in."""
    digest = hashlib.sha256(f"{scope or ''}\n{server.url}".encode()).hexdigest()[:16]
    return TokenStore((home or noeta_home()) / AUTH_DIR / f"{server.alias}-{digest}.json")


# ---------------------------------------------------------------- tokens


def _client_auth(client: Mapping[str, Any], methods: list[str], body: dict[str, str]
                 ) -> dict[str, str]:
    """Put the client's credentials on a token request; returns its headers."""
    secret = str(client.get("client_secret") or "")
    client_id = str(client.get("client_id") or "")
    if secret and (not methods or "client_secret_basic" in methods):
        raw = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(secret, safe='')}"
        return {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()}
    body["client_id"] = client_id
    if secret:
        body["client_secret"] = secret
    return {}


class TokenRefused(McpError):
    """The authorization server answered a token request with an error
    (``error``: its OAuth error code, else the HTTP status)."""

    def __init__(self, message: str, *, error: Any) -> None:
        super().__init__(message)
        self.error = error


async def _token_request(http: httpx.AsyncClient, data: Mapping[str, Any],
                         body: dict[str, str]) -> dict[str, Any]:
    endpoints = data.get("endpoints") or {}
    headers = {"Accept": "application/json",
               **_client_auth(data.get("client") or {}, endpoints.get("auth_methods") or [],
                              body)}
    try:
        resp = await http.post(str(endpoints.get("token")), data=body, headers=headers,
                               timeout=_HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise McpError(f"token request failed: {exc}") from exc
    try:
        tokens = resp.json()
    except ValueError:
        tokens = None
    if resp.status_code != 200 or not isinstance(tokens, dict) \
            or not tokens.get("access_token"):
        detail = tokens.get("error") if isinstance(tokens, dict) else resp.status_code
        raise TokenRefused(f"token request refused ({detail})", error=detail)
    expires = tokens.get("expires_in")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool):
        tokens["expires_at"] = time.time() + float(expires)
    return tokens


class OAuth:
    """The bearer token of one connection: read from the store, renewed
    when it is about to expire or was refused."""

    def __init__(self, server: McpServer, store: TokenStore,
                 http: httpx.AsyncClient | None = None) -> None:
        self.server = server
        self.store = store
        self._http = http
        self._lock = asyncio.Lock()

    async def header(self) -> str | None:
        """``Bearer <token>``, or ``None`` before anyone signed in."""
        async with self._lock:
            data = self.store.load()
            tokens = data.get("tokens") or {}
            if not tokens.get("access_token"):
                return None
            expires = tokens.get("expires_at")
            if isinstance(expires, (int, float)) and expires - time.time() < REFRESH_MARGIN_S:
                tokens = await self._refresh(tokens) or tokens
            return f"Bearer {tokens['access_token']}"

    async def refused(self, sent: str | None) -> bool:
        """The server refused ``sent``: renew the token. ``True`` when there
        is a new one worth one more try."""
        async with self._lock:
            data = self.store.load()
            tokens = data.get("tokens") or {}
            current = f"Bearer {tokens['access_token']}" if tokens.get("access_token") else None
            if current is not None and current != sent:
                return True  # another connection renewed it meanwhile
            return await self._refresh(tokens) is not None

    async def _refresh(self, seen: Mapping[str, Any]) -> dict[str, Any] | None:
        """Renew the tokens ``seen`` holds: the new ones, or ``None``. Under
        the file's lock from the read to the write, so another connection
        or process renewing meanwhile is found instead of spending a
        rotated refresh token twice."""
        async with self.store.locked_async():
            data = self.store.load()
            tokens = data.get("tokens") or {}
            if tokens.get("access_token") and tokens.get("access_token") != seen.get(
                    "access_token"):
                return dict(tokens)  # renewed elsewhere meanwhile
            refresh = tokens.get("refresh_token")
            if not refresh or not (data.get("endpoints") or {}).get("token"):
                return None
            body = {"grant_type": "refresh_token", "refresh_token": str(refresh)}
            resource = (data.get("endpoints") or {}).get("resource")
            if resource:
                body["resource"] = str(resource)
            try:
                async with _client(self._http) as http:
                    fresh = await _token_request(http, data, body)
            except TokenRefused as exc:
                if exc.error == "invalid_grant":  # the refresh token is spent
                    self.store.save({**data, "tokens": {}})
                return None
            except McpError:  # the network: try again later
                return None
            fresh.setdefault("refresh_token", refresh)
            self.store.save({**data, "tokens": fresh})
            return fresh


@contextlib.asynccontextmanager
async def _client(http: httpx.AsyncClient | None) -> Any:
    if http is not None:
        yield http
        return
    async with httpx.AsyncClient(follow_redirects=True, mounts=DIRECT_LOOPBACK) as own:
        yield own


# ---------------------------------------------------------------- sign-in


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def callback_port(configured: int | None = None) -> int:
    """The redirect port: the environment's, else ``configured`` when it is
    free, else a free one in the dynamic range, else 3118."""
    raw = os.environ.get(CALLBACK_PORT_ENV, "").strip()
    if raw.isdigit() and 0 < int(raw) <= 65535:
        return int(raw)
    if configured and _port_free(configured):
        return configured
    low, high = _PORT_RANGE
    for _ in range(_PORT_TRIES):
        port = random.randint(low, high)
        if _port_free(port):
            return port
    return _FALLBACK_PORT


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class SignIn:
    """One authorization-code sign-in: :meth:`start` returns the URL the
    person opens, :meth:`finish` waits for the browser (or a pasted
    :meth:`submit`) and stores the tokens."""

    def __init__(self, server: McpServer, store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, challenge: str = "",
                 timeout: float = SIGN_IN_TIMEOUT_S, client_name: str = "noeta") -> None:
        self.server = server
        self.store = store
        self.challenge = challenge
        self.timeout = timeout
        self.client_name = client_name
        self._http = http
        self._state = secrets.token_urlsafe(24)
        self._verifier, self._code_challenge = _pkce()
        self._code: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._listener: asyncio.AbstractServer | None = None
        self.redirect_uri = ""
        self.url = ""

    async def start(self) -> str:
        """Discover, register when needed, listen for the redirect; the
        authorization URL."""
        settings = self.server.oauth or OAuthSettings()
        port = callback_port(settings.callback_port)
        self.redirect_uri = f"http://localhost:{port}/callback"
        async with _client(self._http) as http:
            endpoints = await discover(http, self.server, self.challenge)
            client = await self._client(http, endpoints, settings)
        await self.store.update_async(endpoints=endpoints.__dict__, client=client)
        try:
            self._listener = await asyncio.start_server(self._on_redirect, "127.0.0.1", port)
        except OSError as exc:
            raise McpError(f"OAuth callback port {port} is unavailable: {exc}") from exc
        query = {"response_type": "code", "client_id": client["client_id"],
                 "redirect_uri": self.redirect_uri, "state": self._state,
                 "code_challenge": self._code_challenge, "code_challenge_method": "S256"}
        if endpoints.scopes:
            query["scope"] = endpoints.scopes
        if endpoints.resource:
            query["resource"] = endpoints.resource
        sep = "&" if "?" in endpoints.authorization else "?"
        self.url = endpoints.authorization + sep + urllib.parse.urlencode(query)
        return self.url

    async def _client(self, http: httpx.AsyncClient, endpoints: Endpoints,
                      settings: OAuthSettings) -> dict[str, Any]:
        saved = self.store.load().get("client") or {}
        if settings.client_id:
            secret = saved.get("client_secret") if saved.get("client_id") == settings.client_id \
                else None
            return {"client_id": settings.client_id,
                    **({"client_secret": secret} if secret else {})}
        if saved.get("client_id") and self.redirect_uri in (saved.get("redirect_uris") or []):
            return dict(saved)
        if not endpoints.registration:
            raise McpError(f"MCP server {self.server.alias!r}: the authorization server "
                           "offers no client registration; set oauth.clientId")
        body: dict[str, Any] = {
            "client_name": f"{self.client_name} ({self.server.alias})",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}
        if endpoints.scopes:
            body["scope"] = endpoints.scopes
        try:
            resp = await http.post(endpoints.registration, json=body, timeout=_HTTP_TIMEOUT_S)
            registered = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise McpError(f"client registration failed: {exc}") from exc
        if resp.status_code not in (200, 201) or not isinstance(registered, dict) \
                or not registered.get("client_id"):
            raise McpError(f"client registration refused ({resp.status_code})")
        return {"client_id": str(registered["client_id"]),
                **({"client_secret": str(registered["client_secret"])}
                   if registered.get("client_secret") else {}),
                "redirect_uris": [self.redirect_uri]}

    def submit(self, callback_url: str) -> bool:
        """Hand over the redirect URL the person copied from the browser.
        ``False`` when it is not this sign-in's (another ``state``) or
        carries neither a code nor an error."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(callback_url).query)
        return self._accept({k: v[0] for k, v in query.items() if v})

    def _accept(self, query: Mapping[str, str]) -> bool:
        if query.get("state") != self._state or self._code.done():
            return False
        if query.get("error"):
            reason = query.get("error_description") or query["error"]
            self._code.set_exception(McpError(f"authorization refused: {reason}"))
            return True
        if not query.get("code"):
            return False
        self._code.set_result(query["code"])
        return True

    async def _on_redirect(self, reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            parts = line.decode("latin-1").split()
            target = urllib.parse.urlsplit(parts[1]) if len(parts) >= 2 else None
            query = {k: v[0] for k, v in urllib.parse.parse_qs(
                target.query if target else "").items() if v}
            ok = target is not None and target.path == "/callback" and self._accept(query)
            failed = not ok or self._code.exception() is not None
            reason = html.escape(str(query.get("error", "unknown request"))[:200])
            body = (_FAILED_PAGE + reason.encode()
                    if failed else _DONE_PAGE)
            status = b"200 OK" if not failed else b"400 Bad Request"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html; charset=utf-8"
                         b"\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except (OSError, TimeoutError, UnicodeError, asyncio.InvalidStateError):
            pass
        finally:
            writer.close()

    async def finish(self) -> None:
        """Wait for the code, trade it for tokens, store them. Raises
        ``McpError`` when the person does not sign in in time or the server
        refuses."""
        try:
            async with asyncio.timeout(self.timeout):
                code = await asyncio.shield(self._code)
        except TimeoutError:
            raise McpError("Authentication timeout") from None
        finally:
            await self.close()
        data = self.store.load()
        body = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": self.redirect_uri, "code_verifier": self._verifier}
        resource = (data.get("endpoints") or {}).get("resource")
        if resource:
            body["resource"] = str(resource)
        async with _client(self._http) as http:
            tokens = await _token_request(http, data, body)
        await self.store.update_async(tokens=tokens)

    async def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
            with contextlib.suppress(Exception):
                await listener.wait_closed()
        if not self._code.done():
            self._code.cancel()
