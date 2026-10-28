"""Tenants: a task carries its tenant, its children inherit it, a scoped
client reaches only its tenant's tasks, and a task posts and lists only
within its own tenant. An old store gets the column with every row in the
default tenant."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import _SCHEMA, SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import UnknownTask
from noeta.core.events import DEFAULT_TENANT, External, ModelReplied, Recorded, TaskCreated
from noeta.core.extension import Extension, TaskView
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider, reply
from tests.conftest import Pg


async def _no_sleep(_: float) -> None:
    return None


async def test_a_scoped_client_reaches_only_its_tenant(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    host = Client(store)
    a, b = host.scoped("a"), host.scoped("b")
    mine = await a.create("main", "hi", dedupe_key="k")
    theirs = await b.create("main", "hi", dedupe_key="k")  # keys are per tenant
    assert mine != theirs
    assert (await host.get(mine)).tenant == "a" and (await host.get(theirs)).tenant == "b"
    assert [r.task_id for r in await a.tasks()] == [mine]
    assert {r.task_id for r in await host.tasks()} == {mine, theirs}
    for call in (b.get(mine), b.events(mine), b.send(mine, "x"), b.answer(mine, "r", "allow"),
                 b.signal(mine, "s"), b.cancel(mine), b.wait(mine, timeout=1),
                 b.create("main", history_from=mine), b.create("main", task_id=mine)):
        with pytest.raises(UnknownTask):
            await call
    with pytest.raises(UnknownTask):
        async for _ in b.watch(mine):
            pass
    assert len(await store.inbox(mine)) == 1  # nothing of b's landed
    assert (await host.get(await host.create("main"))).tenant == DEFAULT_TENANT
    await store.close()


class Reach:
    """Spawns a child, posts to the task named in its input, lists tasks."""

    name = "Reach"
    description = "reach"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.post is not None and ctx.tasks is not None and ctx.spawn is not None
        ctx.spawn("child", "do it")
        self.seen = [r.task_id for r in await ctx.tasks(limit=50)]
        try:
            await ctx.post(External(name="poke"), task_id=input["to"])
        except UnknownTask:
            return text_output("refused")
        return text_output("posted")


async def test_a_task_posts_and_lists_only_within_its_tenant(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    reach = Reach()
    provider = ScriptedProvider()
    agents = {"main": Agent(name="main", model="m", tools=[reach]),
              "child": Agent(name="child", model="m")}
    rt = Runtime(store=store, agents=agents, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    host = Client(store)
    other = await host.scoped("b").create("main")
    neighbour = await host.scoped("a").create("main")
    provider.add(reply("", ("Reach", {"to": other})), reply("child done"), reply("ok"))
    tid = await host.scoped("a").create("main", "go")
    await rt.run_until_idle()
    finished = [e.event for e in await store.read(tid) if type(e.event).__name__ == "ToolFinished"]
    assert finished[0].result.text == "refused"  # type: ignore[attr-defined]
    assert other not in reach.seen and neighbour in reach.seen
    assert not any(isinstance(i.body, External) for i in await store.inbox(other))
    [child] = await store.tasks(parent_id=tid)
    assert child.tenant == "a"
    created = (await store.read(child.task_id))[0].event
    assert isinstance(created, TaskCreated) and created.tenant == "a"

    provider.add(reply("", ("Reach", {"to": neighbour})), reply("child done"), reply("ok"))
    await host.send(tid, "again")
    await rt.run_until_idle()
    assert any(isinstance(i.body, External) for i in await store.inbox(neighbour))
    await store.close()


class Relay(Extension):
    """On each model reply, enqueues an ``External`` to every task named in
    the task's config (``to``)."""

    name = "relay"

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        if isinstance(recorded.event, ModelReplied):
            for target in view.state.config.get("to", []):
                view.enqueue(External(name="news"), dedupe_key=f"news:{view.task_id}",
                             task_id=target)


async def test_an_extension_posts_to_another_task_only_within_its_tenant(
        tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("done")])
    agents = {"main": Agent(name="main", model="m", extensions=[Relay()]),
              "idle": Agent(name="idle", model="m")}
    rt = Runtime(store=store, agents=agents, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    host = Client(store)
    other = await host.scoped("b").create("idle")
    neighbour = await host.scoped("a").create("idle")
    await host.scoped("a").create("main", "go", config={"to": [other, neighbour, "nobody"]})
    await rt.run_until_idle()
    news = [i for i in await store.inbox(neighbour)
            if isinstance(i.body, External) and i.body.name == "news"]
    assert len(news) == 1
    assert not any(isinstance(i.body, External) for i in await store.inbox(other))
    await store.close()


def test_an_old_sqlite_store_gets_the_tenant_column(tmp_path: Path) -> None:
    import asyncio

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tasks (task_id, root_id, agent, queue, status, last_seq,"
                 " created_at, updated_at) VALUES ('t', 't', 'a', 'default', 'ready', 1, 0, 0)")
    conn.commit()
    conn.close()

    async def check() -> None:
        store = SqliteStore(path)
        row = await store.get("t")
        assert row is not None and row.tenant == DEFAULT_TENANT
        await store.close()

    asyncio.run(check())


async def test_an_old_postgres_schema_gets_the_tenant_column(pg: Pg) -> None:
    import psycopg

    from noeta.backends.stores.postgres import _SCHEMA as PG_SCHEMA

    with psycopg.connect(pg.dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{pg.schema}"')
        conn.execute(PG_SCHEMA.replace("{s}", f'"{pg.schema}"'))
        conn.execute(f'INSERT INTO "{pg.schema}".tasks (task_id, root_id, agent, queue, status,'
                     " last_seq, created_at, updated_at) VALUES ('t', 't', 'a', 'default',"
                     " 'ready', 1, 0, 0)")
    store = pg.store()
    row = await store.get("t")
    assert row is not None and row.tenant == DEFAULT_TENANT
    assert [r.task_id for r in await store.tasks(tenant=DEFAULT_TENANT)] == ["t"]
    await store.close()
