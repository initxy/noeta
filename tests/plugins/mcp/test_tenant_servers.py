"""A task whose tenant is not the default one gets no MCP server that would
run a process on the worker (a stdio server, a ``headersHelper``), and its
connections and sign-ins are its tenant's own."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded
from noeta.core.extension import CommandContext
from noeta.core.runner import Runtime
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.testing import ScriptedProvider, reply


class Recording(McpPool):
    """Lists no tools and connects nowhere; remembers who asked."""

    def __init__(self) -> None:
        super().__init__()
        self.used: list[tuple[str, str | None]] = []

    @contextlib.asynccontextmanager
    async def acquire(self, server: McpServer, scope: str | None = None,
                      context: Any = None) -> AsyncIterator[Any]:
        self.used.append((server.alias, scope))
        yield _NoTools()


class _NoTools:
    async def list_tools(self) -> list[Any]:
        return []

    async def list_prompts(self) -> list[Any]:
        return []


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_a_tenants_task_gets_only_remote_servers_of_its_own_scope(
        store: SqliteStore) -> None:
    pool = Recording()
    servers = [McpServer(alias="local", command=[sys.executable, "-c", "pass"]),
               McpServer(alias="remote", url="http://127.0.0.1:9/mcp"),
               McpServer(alias="helped", url="http://127.0.0.1:9/mcp",
                         headers_helper="echo {}")]
    agent = Agent(name="a", model="m", extensions=[Mcp(servers, pool=pool, scope="s")])
    rt = Runtime(store=store, agents={"a": agent},
                 provider_for=lambda _m: ScriptedProvider([reply("ok")] * 2),
                 env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    await Client(store).create("a", "go")
    await rt.run_until_idle()
    assert sorted(pool.used) == [("helped", "s"), ("local", "s"), ("remote", "s")]
    pool.used.clear()
    acme = Client(store).scoped("acme")
    tid = await acme.create("a", "go")
    await rt.run_until_idle()
    assert pool.used == [("remote", "tenant:acme/s")]
    downs = {e.data["alias"]: e.data["reason"] for e in
             (r.event for r in await acme.events(tid))
             if isinstance(e, ExtensionRecorded) and e.kind == "server_down"}
    assert set(downs) == {"local", "helped"}
    assert all("acme" in reason for reason in downs.values())


async def test_listing_commands_for_a_tenant_starts_no_local_server() -> None:
    pool = Recording()
    mcp = Mcp([McpServer(alias="local", command=[sys.executable, "-c", "pass"]),
               McpServer(alias="remote", url="http://127.0.0.1:9/mcp")], pool=pool)
    await mcp.commands(CommandContext(cwd="/"))
    assert sorted(pool.used) == [("local", None), ("remote", None)]
    pool.used.clear()
    await mcp.commands(CommandContext(cwd="/", tenant="acme"))
    assert pool.used == [("remote", "tenant:acme")]


async def test_the_http_command_listing_carries_the_requests_tenant(tmp_path: Path) -> None:
    pytest.importorskip("starlette")
    httpx = pytest.importorskip("httpx")
    from noeta.host.http import create_app

    pool = Recording()
    agent = Agent(name="main", model="m", extensions=[Mcp(
        [McpServer(alias="local", command=[sys.executable, "-c", "pass"])], pool=pool)])
    store = SqliteStore(str(tmp_path / "h.db"))
    api = create_app(Client(store), token="tok-default", tokens={"tok-a": "acme"},
                     agents={"main": agent}, cwd=str(tmp_path),
                     workspaces={"acme": str(tmp_path)})
    transport = httpx.ASGITransport(app=api)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
            for token, used in (("tok-a", []), ("tok-default", [("local", None)])):
                got = await http.get("/commands?agent=main",
                                     headers={"authorization": f"Bearer {token}"})
                assert got.status_code == 200, got.text
                assert pool.used == used
    finally:
        await store.close()


class _Prompts:
    def __init__(self, scope: str | None) -> None:
        self.scope = scope

    async def list_prompts(self) -> list[Any]:
        return [{"name": "hello"}]

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"messages": [{"role": "user",
                              "content": {"type": "text", "text": f"from {self.scope}"}}]}


class PromptPool(Recording):
    @contextlib.asynccontextmanager
    async def acquire(self, server: McpServer, scope: str | None = None,
                      context: Any = None) -> AsyncIterator[Any]:
        self.used.append((server.alias, scope))
        yield _Prompts(scope)


async def test_a_prompt_runs_in_the_partition_of_the_tenant_that_runs_it() -> None:
    pool = PromptPool()
    mcp = Mcp([McpServer(alias="remote", url="http://127.0.0.1:9/mcp")], pool=pool)
    acme, beta = CommandContext(cwd="/", tenant="acme"), CommandContext(cwd="/", tenant="beta")
    names = [c.name for c in await mcp.commands(acme)]
    await mcp.commands(beta)  # beta lists the same command name after acme
    pool.used.clear()
    assert await mcp.run_command(acme, names[0], "") == "from tenant:acme"
    assert pool.used == [("remote", "tenant:acme")]
    assert await mcp.run_command(beta, names[0], "") == "from tenant:beta"
    assert await mcp.run_command(CommandContext(cwd="/"), names[0], "") == "from None"


async def test_listed_prompts_are_kept_for_a_bounded_number_of_partitions(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.plugins.mcp import extension

    monkeypatch.setattr(extension, "TASKS_KEEP", 2)
    mcp = Mcp([McpServer(alias="remote", url="http://127.0.0.1:9/mcp")], pool=PromptPool())
    for tenant in ("a", "b", "c"):
        await mcp.commands(CommandContext(cwd="/", tenant=tenant))
    assert {scope for scope, _ in mcp._prompts} == {"tenant:b", "tenant:c"}
    # A partition let go lists its prompts again when it runs one.
    assert await mcp.run_command(CommandContext(cwd="/", tenant="a"),
                                 "mcp__remote__hello", "") == "from tenant:a"
