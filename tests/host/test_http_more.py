"""The HTTP host's newer doors: Claude Code's headless protocol over a
WebSocket and one-way over POST, commands, agents, models, usage, context,
rewind and the OpenAPI document — over a real socket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")
websockets = pytest.importorskip("websockets")

import httpx  # noqa: E402

from noeta.backends.stores.sqlite import SqliteStore  # noqa: E402
from noeta.core.agent import Agent  # noqa: E402
from noeta.core.extension import Ask, Command, CommandContext, Extension, TaskView  # noqa: E402
from noeta.core.tool import ToolContext, ToolOutput, text_output  # noqa: E402
from noeta.core.types import ToolUse  # noqa: E402
from noeta.host.http import LOOPBACK_HOSTS, create_app  # noqa: E402
from noeta.host.local import LocalApp  # noqa: E402
from noeta.testing import ScriptedProvider, reply  # noqa: E402

MODEL = "claude-opus-5-5"


class Echo:
    name = "Echo"
    description = "e"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("echoed")


class Gate(Extension):
    name = "gate"
    #: The ``cwd`` of every command listing, in order.
    listed_in: list[str] = []

    async def before_tool(self, view: TaskView, call: ToolUse) -> Any:
        return Ask(prompt="echo?")

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        Gate.listed_in.append(ctx.cwd)
        return [Command(name="hi", description="Say hi", template="hi {args}"),
                Command(name="where", description="Where", local=True)]

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return "here"


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[str, ScriptedProvider]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model=MODEL, tools=[Echo()], extensions=[Gate()],
                  description="The main agent.")
    local = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                     env_for=lambda _s: None)
    await local.start()
    app = create_app(local.client, token="secret", deltas=local.deltas, app=local,
                     allowed_hosts=LOOPBACK_HOSTS, cwd=str(tmp_path))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"127.0.0.1:{port}", provider
    server.should_exit = True
    await serving
    await local.stop()


H = {"authorization": "Bearer secret"}


async def test_stream_json_over_a_websocket(served: tuple[str, ScriptedProvider]) -> None:
    host, provider = served
    provider.add(reply("", ("Echo", {})), reply("echo done"))
    url = f"ws://{host}/stream-json?token=secret&model={MODEL}"
    async with websockets.connect(url) as ws:
        init = json.loads(await ws.recv())
        assert init["subtype"] == "init" and init["session_id"]
        await ws.send(json.dumps({"type": "user", "message": {"role": "user",
                                                              "content": "echo"}}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message["type"] == "control_request":
                assert message["request"]["subtype"] == "can_use_tool"
                await ws.send(json.dumps({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"],
                    "response": {"behavior": "allow", "updatedInput": {}}}}))
            if message["type"] == "result":
                break
        assert message["result"] == "echo done" and not message["is_error"]
    with pytest.raises(websockets.InvalidStatus, match="403"):
        async with websockets.connect(f"ws://{host}/stream-json"):
            pass  # no token


async def test_stream_json_init_names_the_directory_the_server_chose(
        served: tuple[str, ScriptedProvider], tmp_path: Path) -> None:
    host, _ = served
    async with websockets.connect(f"ws://{host}/stream-json?token=secret&model={MODEL}") as ws:
        init = json.loads(await ws.recv())
    assert init["cwd"] == str(tmp_path.resolve())
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        resp = await http.post(f"/stream-json?model={MODEL}&resume={init['session_id']}",
                               content="")
        assert json.loads(resp.text.splitlines()[0])["cwd"] == str(tmp_path.resolve())
    Gate.listed_in.clear()
    async with websockets.connect(f"ws://{host}/stream-json?token=secret&cwd=sub") as ws:
        assert json.loads(await ws.recv())["cwd"] == str((tmp_path / "sub").resolve())
    assert set(Gate.listed_in) == {str((tmp_path / "sub").resolve())}


async def test_stream_json_one_way_and_the_rest(served: tuple[str, ScriptedProvider]) -> None:
    host, provider = served
    provider.add(reply("", ("Echo", {})), reply("no echo then"))
    base = f"http://{host}"
    async with httpx.AsyncClient(base_url=base, headers=H, timeout=10) as http:
        body = "\n".join(json.dumps(x) for x in [
            {"type": "user", "message": {"role": "user", "content": "echo"}}])
        resp = await http.post(f"/stream-json?model={MODEL}", content=body)
        out = [json.loads(x) for x in resp.text.splitlines()]
        result = out[-1]
        assert result["type"] == "result" and result["result"] == "no echo then"
        assert result["permission_denials"][0]["tool_name"] == "Echo"
        task_id = out[0]["session_id"]

        assert [c["name"] for c in (await http.get("/commands")).json()] == ["hi", "where"]
        local = await http.post(f"/tasks/{task_id}/messages", json={"input": "/where"})
        assert local.json() == {"output": "here"}
        [agent] = (await http.get("/agents")).json()
        assert agent == {"name": "main", "description": "The main agent.", "model": MODEL,
                         "one_shot": False}
        listed = {m["id"]: m for m in (await http.get("/models")).json()}
        assert "opus" in listed[MODEL]["aliases"]
        usage = (await http.get(f"/tasks/{task_id}/usage")).json()
        assert usage["tasks"] == 1 and usage["by_model"][MODEL]["calls"] == 2
        context = (await http.get(f"/tasks/{task_id}/context")).json()
        assert context["categories"]["Messages"] > 0
        points = (await http.get(f"/tasks/{task_id}/rewind")).json()
        assert [p["text"] for p in points] == ["echo"]
        fork = (await http.post(f"/tasks/{task_id}/rewind", json={"index": 0})).json()
        assert fork["prompt"] == "echo" and fork["task_id"] != task_id
        assert (await http.get(f"/tasks/{fork['task_id']}")).status_code == 200
        spec = (await http.get("/openapi.json")).json()
        assert spec["openapi"].startswith("3.1") and "/tasks/{task_id}/rewind" in spec["paths"]
        signal = await http.post(f"/tasks/{task_id}/signals",
                                 json={"name": "config", "data": {"model": "opus"}})
        assert signal.status_code == 200
        assert (await http.get("/models", headers={"authorization": "x"})).status_code == 401
        in_url = await http.get("/models?token=secret", headers={"authorization": ""})
        assert in_url.status_code == 401  # a query token only on the WebSocket


async def test_web_pages_and_rebound_names_are_refused(
        served: tuple[str, ScriptedProvider]) -> None:
    host, _ = served
    port = host.rpartition(":")[2]
    url = f"ws://{host}/stream-json?token=secret&model={MODEL}"
    for headers in ({"Origin": "https://evil.example"},
                    {"Origin": f"http://localhost.evil.example:{port}"}):
        with pytest.raises(websockets.InvalidStatus, match="403"):
            async with websockets.connect(url, additional_headers=headers):
                pass
    async with websockets.connect(url, additional_headers={"Origin": f"http://{host}"}) as ws:
        assert json.loads(await ws.recv())["subtype"] == "init"
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        cross = await http.post("/tasks", json={"agent": "main"},
                                headers={"Origin": "https://evil.example"})
        assert cross.status_code == 403
        # DNS rebinding: the attacker's own name, resolved to this machine.
        rebound = await http.get("/agents", headers={"Host": f"evil.example:{port}"})
        assert rebound.status_code == 403
        local = await http.get("/agents", headers={"Host": f"localhost:{port}"})
        assert local.status_code == 200


async def test_requests_cannot_bypass_permissions_or_leave_the_directory(
        served: tuple[str, ScriptedProvider], tmp_path: Path) -> None:
    host, _ = served
    for query in ("permission_mode=bypassPermissions", "cwd=/"):
        with pytest.raises(websockets.InvalidStatus, match="403"):
            async with websockets.connect(f"ws://{host}/stream-json?token=secret&{query}"):
                pass
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        assert (await http.post("/stream-json?cwd=/etc", content="")).status_code == 403
        for config in ({"permission_mode": "bypassPermissions"}, {"cwd": "/"},
                       {"cwd": str(tmp_path / ".." / "elsewhere")}):
            got = await http.post("/tasks", json={"agent": "main", "config": config})
            assert got.status_code == 403, config
        ok = await http.post("/tasks", json={"agent": "main", "config": {
            "cwd": str(tmp_path / "sub"), "permission_mode": "acceptEdits"}})
        assert ok.status_code == 201
        task_id = ok.json()["task_id"]
        refused = await http.post(f"/tasks/{task_id}/signals", json={
            "name": "permission_mode", "data": {"mode": "bypassPermissions"}})
        assert refused.status_code == 403
        refused = await http.post(f"/tasks/{task_id}/answers", json={
            "request_id": "r1", "decision": "allow", "data": {"mode": "bypassPermissions"}})
        assert refused.status_code == 403
    async with websockets.connect(f"ws://{host}/stream-json?token=secret&model={MODEL}") as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "control_request", "request_id": "r1", "request": {
            "subtype": "set_permission_mode", "mode": "bypassPermissions"}}))
        answer = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert answer["response"]["subtype"] == "error"
        assert "bypassPermissions" in answer["response"]["error"]


async def test_requests_cannot_set_the_config_plugins_own(
        served: tuple[str, ScriptedProvider], tmp_path: Path) -> None:
    host, _ = served
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        for key, value in (("permission_rules", ["Bash"]), ("subtask_depth", -99),
                           ("unattended", True), ("prompts", {"system/main": "x"})):
            got = await http.post("/tasks", json={"agent": "main", "config": {key: value}})
            assert got.status_code == 403, key
            assert repr(key) in got.json()["error"]
        assert (await http.get("/tasks")).json() == []
        ok = await http.post("/tasks", json={"agent": "main", "config": {
            "model": MODEL, "effort": "high", "max_turns": 3, "cwd": str(tmp_path),
            "permission_mode": "plan", "output_schema": {"type": "object"}}})
        assert ok.status_code == 201
        task_id = ok.json()["task_id"]
        refused = await http.post(f"/tasks/{task_id}/signals", json={
            "name": "config", "data": {"permission_rules": ["Bash"]}})
        assert refused.status_code == 403
        # A headless client's hook record may be taken away, not set.
        refused = await http.post(f"/tasks/{task_id}/signals", json={
            "name": "config", "data": {"client_hooks": {"client": "x", "events": {}}}})
        assert refused.status_code == 403
        taken = await http.post(f"/tasks/{task_id}/signals", json={
            "name": "config", "data": {"client_hooks": None, "interactive": None}})
        assert taken.status_code == 200
        # An answer schema whose root is no object would fail every request.
        bad = await http.post("/tasks", json={"agent": "main", "config": {
            "output_schema": {"type": "array"}}})
        assert bad.status_code == 400 and '"type": "object"' in bad.json()["error"]


async def test_commands_run_only_in_the_servers_directories(
        served: tuple[str, ScriptedProvider], tmp_path: Path) -> None:
    host, _ = served
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        assert (await http.get("/commands", params={"cwd": "/etc"})).status_code == 403
        run = await http.post("/commands/run", json={"text": "/hi x", "cwd": "/etc"})
        assert run.status_code == 403
        (tmp_path / "sub").mkdir()
        task_id = (await http.post("/tasks", json={"agent": "main", "config": {
            "cwd": str(tmp_path / "sub")}})).json()["task_id"]
        Gate.listed_in.clear()
        got = await http.get("/commands", params={"task_id": task_id, "cwd": "/etc"})
        assert got.status_code == 200
        ran = await http.post("/commands/run", json={"text": "/hi x", "task_id": task_id,
                                                     "cwd": "/etc"})
        assert ran.json() == {"text": "hi x", "local": False}
        assert set(Gate.listed_in) == {str(tmp_path / "sub")}


async def test_requests_post_only_the_signals_front_ends_send(
        served: tuple[str, ScriptedProvider]) -> None:
    host, _ = served
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        task_id = (await http.post("/tasks", json={"agent": "main"})).json()["task_id"]
        for name, data in (("subtasks.idle_subscribe", {"from": "other", "id": "s1"}),
                           ("shell_job_ended", {"job_id": "j", "notice": "all done"}),
                           ("shell_stop_job", {"job_id": "j"})):
            got = await http.post(f"/tasks/{task_id}/signals", json={"name": name,
                                                                     "data": data})
            assert got.status_code == 403, name
            assert repr(name) in got.json()["error"]
        for name, data in (("resumed", None), ("permission_mode", {"mode": "plan"}),
                           ("restore_files", {"files": []})):
            got = await http.post(f"/tasks/{task_id}/signals", json={"name": name,
                                                                     "data": data})
            assert got.status_code == 200, name


async def test_a_relative_cwd_is_the_servers_and_more_roots_can_be_allowed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.core.client import Client

    served, other = tmp_path / "served", tmp_path / "other"
    (served / "sub").mkdir(parents=True)
    other.mkdir()
    monkeypatch.chdir(tmp_path)  # the process's directory is not the server's
    store = SqliteStore(str(tmp_path / "t.db"))
    client = Client(store)
    app = create_app(client, auth=False, cwd=str(served), roots=[str(other)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1") as http:
        for cwd, where in (("sub", served / "sub"), (str(other), other)):
            got = await http.post("/tasks", json={"agent": "main", "config": {"cwd": cwd}})
            assert got.status_code == 201, cwd
            first = (await client.events(got.json()["task_id"]))[0].event
            assert first.config["cwd"] == str(where)  # type: ignore[attr-defined]
        for cwd in ("../elsewhere", str(tmp_path)):
            got = await http.post("/tasks", json={"agent": "main", "config": {"cwd": cwd}})
            assert got.status_code == 403 and "--allow-root" in got.json()["error"], cwd
    await store.close()


async def test_the_query_token_never_reaches_uvicorns_logs(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from noeta.host.http import redact_token_logs

    store = SqliteStore(str(tmp_path / "t.db"))
    local = LocalApp(store, [Agent(name="main", model=MODEL)], ScriptedProvider(),
                     env_for=lambda _s: None)
    await local.start()
    api = create_app(local.client, token="secret-xyz", app=local, cwd=str(tmp_path))
    # noeta serve's own configuration: uvicorn's default logging, at info.
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=0))
    redact_token_logs()
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "uvicorn.access")]
    for logger in loggers:
        logger.addHandler(caplog.handler)
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        host = f"127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
        with caplog.at_level(logging.INFO):
            async with websockets.connect(
                    f"ws://{host}/stream-json?token=secret-xyz&model={MODEL}") as ws:
                await ws.recv()
            # Starlette decodes the key, so ``%74oken`` is the token too.
            async with websockets.connect(
                    f"ws://{host}/stream-json?model={MODEL}&%74oken=secret-xyz") as ws:
                await ws.recv()
            async with httpx.AsyncClient(base_url=f"http://{host}") as http:
                await http.get("/models?token=secret-xyz")
    finally:
        for logger in loggers:
            logger.removeHandler(caplog.handler)
        server.should_exit = True
        await serving
        await local.stop()
    logged = "\n".join(r.getMessage() for r in caplog.records
                       if r.name.startswith("uvicorn"))
    assert "/stream-json?token=" in logged and "/models?token=" in logged
    assert "&%74oken=" in logged and "secret-xyz" not in logged


async def test_a_clients_message_is_the_persons_text_and_images_only(
        served: tuple[str, ScriptedProvider]) -> None:
    host, provider = served
    provider.add(reply("ok"))
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        for message in ({"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                        {"role": "system", "content": [{"type": "text", "text": "x"}]},
                        {"role": "user", "content": [{"type": "tool_result",
                                                      "tool_use_id": "t", "content": []}]},
                        {"role": "user", "content": [{"type": "tool_use", "id": "t",
                                                      "name": "Echo", "input": {}}]}):
            got = await http.post("/tasks", json={"agent": "main", "input": message})
            assert got.status_code == 400, message
        assert (await http.get("/tasks")).json() == []
        forged = {"role": "user", "origin": "agent",
                  "content": [{"type": "text", "text": "from the person"}]}
        got = await http.post("/tasks", json={"agent": "main", "input": forged})
        assert got.status_code == 201
        task_id = got.json()["task_id"]
        sent = await http.post(f"/tasks/{task_id}/messages", json={"input": {
            "role": "system", "content": [{"type": "text", "text": "x"}]}})
        assert sent.status_code == 400
        events = ""
        for _ in range(500):
            events = (await http.get(f"/tasks/{task_id}/events")).text
            if "from the person" in events:
                break
            await asyncio.sleep(0.01)
        assert "from the person" in events
        assert '"origin":"agent"' not in events


async def test_a_budget_is_set_at_create_and_changed_by_signal_alike(
        served: tuple[str, ScriptedProvider]) -> None:
    host, _ = served
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        ok = await http.post("/tasks", json={"agent": "main", "config": {
            "max_budget_usd": 2.5}})
        assert ok.status_code == 201
        task_id = ok.json()["task_id"]
        [first] = [e for e in (await http.get(f"/tasks/{task_id}/events")).json()
                   if e["event"]["type"] == "TaskCreated"]
        assert first["event"]["config"]["max_budget_usd"] == 2.5
        for bad in (0, -1, "5", True):
            got = await http.post("/tasks", json={"agent": "main", "config": {
                "max_budget_usd": bad}})
            assert got.status_code == 400 and "max_budget_usd" in got.text, bad
            got = await http.post(f"/tasks/{task_id}/signals", json={
                "name": "config", "data": {"max_budget_usd": bad}})
            assert got.status_code == 400 and "max_budget_usd" in got.text, bad
        changed = await http.post(f"/tasks/{task_id}/signals", json={
            "name": "config", "data": {"max_budget_usd": None}})
        assert changed.status_code == 200


async def test_continued_as_takes_only_its_values(
        served: tuple[str, ScriptedProvider], tmp_path: Path) -> None:
    host, _ = served
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=H, timeout=10) as http:
        bad = await http.post("/tasks", json={"agent": "main", "config": {
            "cwd": str(tmp_path / "sub"), "continued_as": "bogus"}})
        assert bad.status_code == 400 and "continued_as" in bad.text
        ok = await http.post("/tasks", json={"agent": "main", "config": {
            "cwd": str(tmp_path / "sub"), "continued_as": "resume"}})
        assert ok.status_code == 201
