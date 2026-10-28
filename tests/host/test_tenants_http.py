"""Tenants over HTTP: each token reaches only its tenant's tasks. Tenant B
cannot list, read, watch, message, answer, signal, cancel, fork or run a
command on tenant A's task, nor reach it from inside a task with
SendMessage; the single server token stays the default tenant's."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")

import httpx  # noqa: E402

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.events import Say, ToolFinished  # noqa: E402
from noeta.host.http import create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.host.remote import RemoteClient  # noqa: E402
from noeta.plugins.subtasks import Subtasks  # noqa: E402
from noeta.plugins.subtasks.tool import SendMessageTool  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402

TOKENS = {"tok-a": "a", "tok-b": "b"}


@pytest.fixture(autouse=True)
def _send_message_loaded() -> Iterator[None]:
    SendMessageTool.deferred = False  # type: ignore[misc]
    yield
    SendMessageTool.deferred = True  # type: ignore[misc]


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[str, ScriptedProvider, LocalApp]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model="m", extensions=[Subtasks(())])
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    await local.start()
    api = create_app(local.client, token="tok-default", tokens=TOKENS, deltas=local.deltas,
                     app=local, cwd=str(tmp_path))
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", provider, local
    server.should_exit = True
    await serving
    await local.stop()


def _headers(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_a_tenant_reaches_nothing_of_another(
        served: tuple[str, ScriptedProvider, LocalApp]) -> None:
    url, provider, local = served
    provider.add(reply("hello a"))
    a = RemoteClient(url, token="tok-a")
    mine = await a.create("main", "hi")
    await a.wait(mine, timeout=10)
    assert (await local.client.get(mine)).tenant == "a"
    async with httpx.AsyncClient(base_url=url, headers=_headers("tok-b")) as b:
        assert (await b.get("/tasks")).json() == []
        task = f"/tasks/{mine}"
        for method, path, body in [
                ("GET", task, None), ("GET", f"{task}/events", None),
                ("GET", f"{task}/watch", None), ("GET", f"{task}/pending", None),
                ("GET", f"{task}/usage", None), ("GET", f"{task}/context", None),
                ("GET", f"{task}/rewind", None),
                ("POST", f"{task}/messages", {"input": "from b"}),
                ("POST", f"{task}/answers", {"request_id": "r", "decision": "allow"}),
                ("POST", f"{task}/signals", {"name": "config", "data": {"model": "x"}}),
                ("POST", f"{task}/cancel", {}),
                ("POST", f"{task}/rewind", {"index": 0}),
                ("POST", "/tasks", {"agent": "main", "history_from": mine}),
                ("GET", f"/commands?task_id={mine}", None),
                ("POST", "/commands/run", {"text": "/x", "task_id": mine})]:
            got = await b.request(method, path, json=body)
            assert got.status_code == 404, (method, path, got.status_code)
    async with httpx.AsyncClient(base_url=url, headers=_headers("tok-default")) as d:
        assert (await d.get("/tasks")).json() == []  # the default tenant's own list
        assert (await d.get(f"/tasks/{mine}")).status_code == 404
    async with httpx.AsyncClient(base_url=url, headers=_headers("wrong")) as w:
        assert (await w.get("/tasks")).status_code == 401
    assert [r.task_id for r in await a.tasks()] == [mine]
    says = [i for i in await local.store.inbox(mine) if isinstance(i.body, Say)]
    assert len(says) == 1  # only a's own first message
    await a.close()


async def test_send_message_cannot_cross_tenants(
        served: tuple[str, ScriptedProvider, LocalApp]) -> None:
    url, provider, local = served
    provider.add(reply("a waits"))
    a, b = RemoteClient(url, token="tok-a"), RemoteClient(url, token="tok-b")
    target = await a.create("main", "hi")
    await a.wait(target, timeout=10)
    provider.add(reply("", ("SendMessage", {"to": target, "message": "psst"})), reply("ok"))
    sender = await b.create("main", "message the other one")
    await b.wait(sender, timeout=10)
    [finished] = [e.event for e in await b.events(sender) if isinstance(e.event, ToolFinished)]
    assert finished.result.is_error and "psst" not in str(await a.events(target))
    assert [i for i in await local.store.inbox(target) if isinstance(i.body, Say)
            and "psst" in i.body.message.text] == []
    await a.close()
    await b.close()


def test_the_tokens_file_maps_tokens_to_tenants(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    import stat

    from noeta.cli import config, main, tenants

    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    lookup = tenants.tenant_tokens()
    assert lookup("nothing") is None
    token = tenants.add_token("acme", "ci")
    assert lookup(token) == "acme" and lookup("nothing") is None
    path = tmp_path / "home" / "tokens.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and token not in path.read_text()
    with pytest.raises(ValueError):
        tenants.add_token("Not A Name")
    [entry] = tenants.read_tokens()
    with pytest.raises(SystemExit) as done:
        main.main(["token", "remove", str(entry["sha256"])[:12]])
    assert done.value.code == 0 and lookup(token) is None
    with pytest.raises(SystemExit) as done:
        main.main(["token", "add", "--tenant", "beta"])
    assert done.value.code == 0 and [e["tenant"] for e in tenants.read_tokens()] == ["beta"]
