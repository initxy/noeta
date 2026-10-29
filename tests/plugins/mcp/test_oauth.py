"""OAuth sign-in for remote MCP servers against an in-process authorization
server (an httpx mock transport); the browser's redirect is a real request
to the loopback listener."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import stat
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.runner import Runtime
from noeta.core.types import Image
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.config import McpAuthRequired, McpError, OAuthSettings
from noeta.plugins.mcp.http_client import HttpClient
from noeta.plugins.mcp.oauth import (
    CALLBACK_PORT_ENV,
    OAuth,
    SignIn,
    TokenStore,
    callback_port,
    parse_challenge,
    store_for,
)
from noeta.testing import ScriptedProvider, reply

PRM = "https://srv.test/.well-known/oauth-protected-resource/mcp"


class AuthServer:
    """An MCP endpoint behind OAuth, with its authorization server."""

    def __init__(self, *, registration: bool = True, prm: bool = True) -> None:
        self.registration = registration
        self.prm = prm
        self.valid = {"a1"}
        self.challenges: dict[str, str] = {}
        self.token_bodies: list[dict[str, str]] = []
        self.token_headers: list[dict[str, str]] = []
        self.registered: list[dict[str, Any]] = []
        self.bearers: list[str | None] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == PRM and self.prm:
            return httpx.Response(200, json={"resource": "https://srv.test/mcp",
                                             "authorization_servers": ["https://auth.test"],
                                             "scopes_supported": ["mcp"]})
        if url == "https://auth.test/.well-known/oauth-authorization-server":
            meta = {"issuer": "https://auth.test",
                    "authorization_endpoint": "https://auth.test/authorize",
                    "token_endpoint": "https://auth.test/token"}
            if self.registration:
                meta["registration_endpoint"] = "https://auth.test/register"
            return httpx.Response(200, json=meta)
        if url == "https://auth.test/register":
            body = json.loads(request.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "c1", **body})
        if url == "https://auth.test/token":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.token_bodies.append(form)
            self.token_headers.append(dict(request.headers))
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                if self.challenges.get(form["code"]) != challenge:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "a1", "refresh_token": "r1",
                                                 "expires_in": 3600, "token_type": "Bearer"})
            if form.get("refresh_token") == "r1":
                self.valid.add("a2")
                return httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if url == "https://srv.test/mcp":
            auth = request.headers.get("Authorization")
            self.bearers.append(auth)
            if auth is None or auth.removeprefix("Bearer ") not in self.valid:
                return httpx.Response(401, headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{PRM}", scope="mcp"'})
            body = json.loads(request.content)
            if "id" not in body:
                return httpx.Response(202)
            result: dict[str, Any] = {"tools": [{"name": "secret"}]} \
                if body["method"] == "tools/list" else {"protocolVersion": "2025-11-25"}
            if body["method"] == "tools/call":
                result = {"content": [{"type": "text", "text": "the secret"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": result})
        return httpx.Response(404)

    def authorize(self, url: str) -> str:
        """What the person's browser does: approve, and be sent back."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.challenges["the-code"] = query["code_challenge"]
        return (f"{query['redirect_uri']}?code=the-code&state="
                f"{urllib.parse.quote(query['state'])}")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


SERVER = McpServer(alias="srv", url="https://srv.test/mcp")


def http_for(auth: AuthServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(auth))


def test_parse_challenge() -> None:
    assert parse_challenge('Bearer resource_metadata="https://x/y", scope="a b", error=x') == {
        "resource_metadata": "https://x/y", "scope": "a b", "error": "x"}
    assert parse_challenge('Bearer realm="a\\"b"') == {"realm": 'a"b'}
    assert parse_challenge("") == {}


def test_callback_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CALLBACK_PORT_ENV, "4567")
    assert callback_port(1234) == 4567
    monkeypatch.delenv(CALLBACK_PORT_ENV)
    port = callback_port(None)
    assert 49152 <= port <= 65535 or port == 3118


async def test_sign_in_end_to_end(home: Path) -> None:
    auth = AuthServer()
    http = http_for(auth)
    client = HttpClient(SERVER.url, http=http, auth=OAuth(SERVER, store_for(SERVER), http))
    with pytest.raises(McpAuthRequired) as refused:
        await client.start()
    assert "resource_metadata" in refused.value.challenge
    await client.close()

    store = store_for(SERVER)
    sign_in = SignIn(SERVER, store, http=http, challenge=refused.value.challenge)
    url = await sign_in.start()
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith("https://auth.test/authorize?")
    assert query["client_id"] == "c1" and query["code_challenge_method"] == "S256"
    assert query["scope"] == "mcp" and query["resource"] == "https://srv.test/mcp"
    assert auth.registered[0]["token_endpoint_auth_method"] == "none"
    assert auth.registered[0]["redirect_uris"] == [sign_in.redirect_uri]
    # The browser comes back to the loopback listener.
    async with httpx.AsyncClient() as browser:
        page = await browser.get(auth.authorize(url).replace("localhost", "127.0.0.1"))
    assert page.status_code == 200 and "Authentication complete" in page.text
    await sign_in.finish()
    assert auth.token_bodies[0]["client_id"] == "c1"
    # Kept for the owner only.
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load()["tokens"]["access_token"] == "a1"

    client = HttpClient(SERVER.url, http=http, auth=OAuth(SERVER, store, http))
    await client.start()
    assert [t["name"] for t in await client.list_tools()] == ["secret"]
    assert auth.bearers[-1] == "Bearer a1"
    # An expiring token is renewed before use; a refused one once more.
    store.update(tokens={**store.load()["tokens"], "expires_at": time.time() + 5})
    await client.list_tools()
    assert auth.bearers[-1] == "Bearer a2"
    assert store.load()["tokens"]["refresh_token"] == "r1"  # kept when not reissued
    store.update(tokens={**store.load()["tokens"], "refresh_token": "spent"})
    auth.valid = {"a3"}
    with pytest.raises(McpAuthRequired):
        await client.list_tools()
    assert store.load()["tokens"] == {}
    await client.close()


async def test_a_pasted_redirect_and_a_wrong_state(home: Path) -> None:
    auth = AuthServer()
    http = http_for(auth)
    sign_in = SignIn(SERVER, store_for(SERVER), http=http)
    url = await sign_in.start()
    back = auth.authorize(url)
    assert not sign_in.submit(back.replace("state=", "state=other"))
    assert not sign_in.submit("http://localhost/callback?state=x")
    assert sign_in.submit(back)
    await sign_in.finish()
    assert store_for(SERVER).load()["tokens"]["access_token"] == "a1"


async def test_the_callback_page_escapes_what_it_shows(home: Path) -> None:
    sign_in = SignIn(SERVER, store_for(SERVER), http=http_for(AuthServer()), timeout=5)
    url = await sign_in.start()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
    evil = urllib.parse.quote("<script>alert(1)</script>")
    async with httpx.AsyncClient() as client:
        page = await client.get(f"{sign_in.redirect_uri}?error={evil}&state={state}")
    assert page.status_code == 400
    assert "<script>" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    with pytest.raises(McpError):
        await sign_in.finish()


async def test_a_refusal_and_a_timeout(home: Path) -> None:
    auth = AuthServer()
    http = http_for(auth)
    sign_in = SignIn(SERVER, store_for(SERVER), http=http)
    url = await sign_in.start()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
    assert sign_in.submit(f"http://localhost/callback?error=access_denied&state={state}")
    with pytest.raises(McpError, match="access_denied"):
        await sign_in.finish()
    slow = SignIn(SERVER, store_for(SERVER), http=http, timeout=0.05)
    await slow.start()
    with pytest.raises(McpError, match="timeout"):
        await slow.finish()


async def test_a_configured_client_and_metadata_url(home: Path) -> None:
    auth = AuthServer(registration=False, prm=False)
    http = http_for(auth)
    server = McpServer(alias="srv", url="https://srv.test/mcp", oauth=OAuthSettings(
        client_id="mine", scopes="read write",
        auth_server_metadata_url="https://auth.test/.well-known/oauth-authorization-server"))
    store = store_for(server)
    store.save({"client": {"client_id": "mine", "client_secret": "s3cret"}})
    sign_in = SignIn(server, store, http=http)
    url = await sign_in.start()
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["client_id"] == "mine" and query["scope"] == "read write"
    assert "resource" not in query and not auth.registered
    assert sign_in.submit(auth.authorize(url))
    await sign_in.finish()
    basic = base64.b64encode(b"mine:s3cret").decode()
    assert auth.token_headers[0]["authorization"] == f"Basic {basic}"
    # Without registration and without a client id there is no way in.
    bare = SignIn(SERVER, store_for(SERVER, "other"), http=http)
    with pytest.raises(McpError, match=r"registration|metadata"):
        await bare.start()
    await bare.close()
    insecure = McpServer(alias="srv", url="https://srv.test/mcp", oauth=OAuthSettings(
        auth_server_metadata_url="http://auth.test/meta"))
    with pytest.raises(McpError, match="https"):
        await SignIn(insecure, store_for(insecure), http=http).start()


# ---------------------------------------------------------------- the model's side


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_the_model_signs_in_through_the_stub_tools(home: Path,
                                                         store: SqliteStore) -> None:
    auth = AuthServer()
    pool = McpPool(http=http_for(auth))
    server = McpServer(alias="srv", url="https://srv.test/mcp", deferred=False)

    def second(request: Any) -> Any:
        url = re.search(r"https://auth\.test/authorize\?\S+", repr(request.messages))
        assert url is not None
        return reply("", ("mcp__srv__complete_authentication",
                          {"callback_url": auth.authorize(url.group(0).rstrip("'\\"))}))

    provider = ScriptedProvider([
        reply("", ("mcp__srv__authenticate", {})),
        second,
        reply("", ("mcp__srv__secret", {})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", extensions=[Mcp([server], pool=pool)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)

    real_texts: list[str] = []

    async def capture() -> None:
        for r in await client.events(tid):
            if isinstance(r.event, ToolFinished):
                real_texts.append("".join(b.text for b in r.event.result.content
                                          if not isinstance(b, Image)))

    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await capture()
    try:
        names = [[t.name for t in r.tools if t.name.startswith("mcp__")]
                 for r in provider.requests]
        assert names[0] == ["mcp__srv__authenticate", "mcp__srv__complete_authentication"]
        assert "https://auth.test/authorize?" in real_texts[0]
        assert "Authentication complete" in real_texts[1]
        assert names[2] == ["mcp__srv__secret"]
        assert real_texts[2] == "the secret"
    finally:
        await pool.close()


def test_concurrent_updates_of_one_token_file_lose_nothing(tmp_path: Path) -> None:
    path = tmp_path / "oauth" / "server.json"
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        store = TokenStore(path)  # one store per writer, as two processes have
        try:
            for i in range(25):
                store.update(**{f"w{n}_{i}": i})
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(TokenStore(path).load()) == 8 * 25
    assert sorted(p.name for p in path.parent.iterdir()
                  if not p.name.endswith(".lock")) == ["server.json"]


def _signed_in(tmp_path: Path) -> TokenStore:
    store = TokenStore(tmp_path / "oauth" / "srv.json")
    store.save({"endpoints": {"token": "https://auth.test/token"},
                "client": {"client_id": "c1"},
                "tokens": {"access_token": "a1", "refresh_token": "r1"}})
    return store


async def test_a_refresh_the_network_failed_keeps_the_tokens(tmp_path: Path) -> None:
    store = _signed_in(tmp_path)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        assert await OAuth(SERVER, store, http).refused("Bearer a1") is False
    assert store.load()["tokens"]["refresh_token"] == "r1"
    busy = _signed_in(tmp_path / "busy")
    unavailable = httpx.MockTransport(
        lambda r: httpx.Response(503, json={"error": "temporarily_unavailable"}))
    async with httpx.AsyncClient(transport=unavailable) as http:
        assert await OAuth(SERVER, busy, http).refused("Bearer a1") is False
    assert busy.load()["tokens"]["refresh_token"] == "r1"


async def test_an_invalid_grant_clears_the_tokens(tmp_path: Path) -> None:
    store = _signed_in(tmp_path)
    spent = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    async with httpx.AsyncClient(transport=spent) as http:
        assert await OAuth(SERVER, store, http).refused("Bearer a1") is False
    assert store.load()["tokens"] == {}


async def test_two_connections_refreshing_at_once_keep_the_sign_in(tmp_path: Path) -> None:
    # The server rotates refresh tokens: r1 works once. The second
    # connection must find the first one's new tokens, not spend r1 again.
    store = _signed_in(tmp_path)
    used: list[str] = []

    async def rotating(request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        used.append(form["refresh_token"])
        await asyncio.sleep(0.05)
        if form["refresh_token"] == "r1" and used.count("r1") == 1:
            return httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2"})
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rotating)) as http:
        results = await asyncio.gather(OAuth(SERVER, store, http).refused("Bearer a1"),
                                       OAuth(SERVER, store, http).refused("Bearer a1"))
    assert results == [True, True]
    assert used == ["r1"]
    assert store.load()["tokens"]["access_token"] == "a2"
