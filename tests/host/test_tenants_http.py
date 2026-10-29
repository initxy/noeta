"""Tenants over HTTP: each token reaches only its tenant's tasks. Tenant B
cannot list, read, watch, message, answer, signal, cancel, fork or run a
command on tenant A's task, nor reach it from inside a task with
SendMessage; the single server token stays the default tenant's."""

from __future__ import annotations

import asyncio
import json
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


def _workspaces(base: Path, *names: str) -> dict[str, str]:
    """A workspace directory for each tenant named."""
    for name in names:
        (base / name).mkdir(exist_ok=True)
    return {name: str(base / name) for name in names}


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
                     app=local, cwd=str(tmp_path), workspaces=_workspaces(tmp_path, "a", "b"))
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


async def test_stream_json_cannot_resume_or_fork_another_tenants_task(
        served: tuple[str, ScriptedProvider, LocalApp], monkeypatch: pytest.MonkeyPatch) -> None:
    websockets = pytest.importorskip("websockets")
    url, _, local = served
    theirs = await local.client.scoped("a").create("secret-agent", None)
    adopted: list[str] = []
    real = local.adopt

    async def adopt(task_id: str) -> None:
        adopted.append(task_id)
        await real(task_id)

    monkeypatch.setattr(local, "adopt", adopt)
    base = url.replace("http://", "ws://")
    for query in (f"resume={theirs}", f"resume={theirs}&fork=1"):
        async with websockets.connect(f"{base}/stream-json?token=tok-b&{query}") as ws:
            said = [json.loads(m) async for m in ws]
        assert said == [{"type": "error", "error": f"unknown task {theirs}"}], query
    assert theirs not in adopted
    assert (await local.client.get(theirs)).agent == "secret-agent"


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


async def test_a_tenants_tasks_run_in_its_workspace(tmp_path: Path) -> None:
    from noeta.core.client import Client

    work, other, inner = tmp_path / "acme", tmp_path / "elsewhere", tmp_path / "acme" / "c"
    (work / "sub").mkdir(parents=True)
    other.mkdir()
    inner.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    client = Client(store)
    api = create_app(client, token="tok-default", tokens={**TOKENS, "tok-c": "c"},
                     cwd=str(tmp_path), workspaces={"a": str(work), "c": str(inner)})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://x", headers=_headers("tok-a")) as a:
        made = (await a.post("/tasks", json={"agent": "main"})).json()["task_id"]
        sub = (await a.post("/tasks", json={"agent": "main",
                                            "config": {"cwd": "sub"}})).json()["task_id"]
        for outside in (str(other), str(tmp_path), "../elsewhere"):
            got = await a.post("/tasks", json={"agent": "main", "config": {"cwd": outside}})
            assert got.status_code == 403 and "workspace" in got.json()["error"]
    for tid, where in ((made, work), (sub, work / "sub")):
        created = (await store.read(tid))[0].event
        assert created.config["cwd"] == str(where.resolve())  # type: ignore[attr-defined]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://x") as http:
        # A tenant without a workspace starts nothing, wherever it asks.
        for config in ({"cwd": str(other)}, {"cwd": str(work)}, None):
            got = await http.post("/tasks", json={"agent": "main", "config": config},
                                  headers=_headers("tok-b"))
            assert got.status_code == 403 and "no workspace" in got.json()["error"]
        got = await http.get(f"/commands?cwd={other}", headers=_headers("tok-b"))
        assert got.status_code == 403
        # Another tenant's workspace is out of reach, even inside one's own.
        for cwd in ("c", str(inner), str(inner / "..") + "/c"):
            got = await http.post("/tasks", json={"agent": "main", "config": {"cwd": cwd}},
                                  headers=_headers("tok-a"))
            assert got.status_code == 403 and "another tenant" in got.json()["error"]
        got = await http.post("/tasks", json={"agent": "main"}, headers=_headers("tok-c"))
        assert got.status_code == 201
        # The default tenant (the operator) works anywhere the server does.
        got = await http.post("/tasks", json={"agent": "main", "config": {"cwd": str(inner)}},
                              headers=_headers("tok-default"))
        assert got.status_code == 201
    await store.close()


async def test_a_tenant_creates_tasks_only_on_its_queues(tmp_path: Path) -> None:
    from noeta.core.client import Client

    store = SqliteStore(str(tmp_path / "t.db"))
    api = create_app(Client(store), token="tok-default", tokens={**TOKENS, "tok-c": "c"},
                     cwd=str(tmp_path), queues={"c": ["gpu"]},
                     workspaces=_workspaces(tmp_path, "a", "b", "c"))
    expected = {("tok-a", "default"): 201, ("tok-a", "a"): 201, ("tok-a", "b"): 403,
                ("tok-a", "gpu"): 403, ("tok-b", "a"): 403, ("tok-c", "gpu"): 201,
                ("tok-c", "default"): 403, ("tok-c", "c"): 403,
                ("tok-default", "a"): 201, ("tok-default", "anything"): 201}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://x") as http:
        for (token, queue), status in expected.items():
            got = await http.post("/tasks", json={"agent": "main", "queue": queue},
                                  headers=_headers(token))
            assert got.status_code == status, (token, queue, got.text)
    await store.close()


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
    assert done.value.code == 1 and tenants.read_tokens() == []  # beta has no workspace
    (tmp_path / "home" / "tenants" / "beta").mkdir(parents=True)
    (tmp_path / "home" / "tenants" / "beta" / "config.toml").write_text(
        f'workspace = "{tmp_path}"\n')
    with pytest.raises(SystemExit) as done:
        main.main(["token", "add", "--tenant", "beta"])
    assert done.value.code == 0 and [e["tenant"] for e in tenants.read_tokens()] == ["beta"]
