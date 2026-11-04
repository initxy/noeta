"""What the web app reads besides the client API: the root tasks (titles,
directories, first prompts, a rename), file matches for ``@`` mentions, and
the built bundle served beside the API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("starlette")

import httpx

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.extension import CommandContext
from noeta.host.files import file_matches
from noeta.host.http import create_app
from noeta.host.local import LocalApp
from noeta.host.pending import Pending, pending_tree
from noeta.host.remote import RemoteApp
from noeta.plugins.title import Title
from noeta.testing import ScriptedProvider, reply

H = {"authorization": "Bearer secret"}


@pytest.fixture
async def local(tmp_path: Path) -> AsyncIterator[tuple[LocalApp, ScriptedProvider]]:
    provider = ScriptedProvider()
    agent = Agent(name="main", model="m", extensions=[Title()])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                   env_for=lambda _s: None)
    await app.start()
    yield app, provider
    await app.stop()


def _http(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
                             base_url="http://127.0.0.1", headers=H, timeout=10)


async def _until(check: object, tries: int = 300) -> object:
    for _ in range(tries):
        got = await check()  # type: ignore[operator]
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_summaries_list_roots_with_their_title_cwd_and_prompt(
        local: tuple[LocalApp, ScriptedProvider], tmp_path: Path) -> None:
    app, provider = local
    provider.add(reply("hi"))
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with _http(api) as http:
        # Not followed live: no title asked of the model, so the rename is the only one.
        made = await http.post("/tasks", json={"agent": "main", "input": "fix the parser",
                                               "config": {"interactive": False}})
        task_id = made.json()["task_id"]
        await app.client.create("main", None, config={"interactive": False},
                                task_id="child-1")  # a root too, without a prompt
        renamed = await http.post(f"/tasks/{task_id}/signals",
                                  json={"name": "rename", "data": {"title": "Parser fix"}})
        assert renamed.status_code == 200

        async def titled() -> list[dict[str, object]]:
            rows = (await http.get("/summaries")).json()
            return [r for r in rows if r["title"] == "Parser fix"]

        [row] = await _until(titled)  # type: ignore[misc]
        assert row["task_id"] == task_id and row["prompt"] == "fix the parser"
        assert row["cwd"] == str(tmp_path) and row["agent"] == "main"
        listed = (await http.get("/summaries", params={"limit": 1})).json()
        assert len(listed) == 1


async def test_an_archived_session_is_listed_apart_over_http_and_in_process(
        local: tuple[LocalApp, ScriptedProvider], tmp_path: Path) -> None:
    app, _ = local
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    kept = await app.client.create("main", None, config={"interactive": False})
    gone = await app.client.create("main", None, config={"interactive": False})
    async with _http(api) as http:
        posted = await http.post(f"/tasks/{gone}/signals",
                                 json={"name": "archive", "data": {"archived": True}})
        assert posted.status_code == 200

        def ids(rows: list[dict[str, Any]]) -> list[str]:
            return [r["task_id"] for r in rows]

        assert ids((await http.get("/summaries", params={"archived": "false"})).json()) == [kept]
        archived = (await http.get("/summaries", params={"archived": "true"})).json()
        assert ids(archived) == [gone] and archived[0]["archived"] is True
        assert set(ids((await http.get("/summaries")).json())) == {kept, gone}
        assert (await http.get("/summaries", params={"archived": "maybe"})).status_code == 400
    remote = RemoteApp("http://127.0.0.1", token="secret",
                       transport=httpx.ASGITransport(app=api))
    await remote.archive(gone, archived=False)
    assert set(ids(await remote.summaries(archived=False))) == {kept, gone}
    await remote.archive(kept)
    assert ids(await remote.summaries(archived=True)) == [kept]
    await remote.stop()
    assert ids(await app.summaries(archived=True)) == [kept]
    await app.archive(kept, archived=False)
    assert await app.summaries(archived=True) == []


async def test_file_matches_rank_names_first_and_skip_build_dirs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    for name in ("src/parser.py", "src/old_parser.py", "docs.md", "node_modules/parser.js"):
        (tmp_path / name).write_text("x")
    assert file_matches(str(tmp_path), "parser") == ["src/parser.py", "src/old_parser.py"]
    assert file_matches(str(tmp_path), "") == ["docs.md", "src/parser.py",
                                               "src/old_parser.py"]
    assert file_matches(str(tmp_path / "missing"), "x") == []


async def test_files_are_listed_only_in_the_servers_directories(
        local: tuple[LocalApp, ScriptedProvider], tmp_path: Path) -> None:
    app, _ = local
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "notes.txt").write_text("x")
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path / "work"))
    async with _http(api) as http:
        assert (await http.get("/files", params={"q": "note"})).json() == ["notes.txt"]
        assert (await http.get("/files", params={"q": "x", "cwd": "/etc"})).status_code == 403
        task_id = (await http.post("/tasks", json={"agent": "main", "config": {
            "interactive": False}})).json()["task_id"]
        got = await http.get("/files", params={"q": "notes", "task_id": task_id})
        assert got.json() == ["notes.txt"]
    remote = RemoteApp("http://127.0.0.1", token="secret",
                       transport=httpx.ASGITransport(app=api))
    assert await remote.files("note", CommandContext(cwd="")) == ["notes.txt"]
    assert [s["task_id"] for s in await remote.summaries()] == [task_id]
    await remote.stop()


async def test_the_web_bundle_is_served_without_a_token_beside_the_api(
        local: tuple[LocalApp, ScriptedProvider], tmp_path: Path) -> None:
    app, _ = local
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "dist" / "index.html").write_text("<html>noeta</html>")
    (tmp_path / "dist" / "assets" / "app.js").write_text("console.log(1)")
    api = create_app(app.client, token="secret", app=app, web_dir=str(tmp_path / "dist"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://127.0.0.1") as http:
        page = await http.get("/")
        assert page.text == "<html>noeta</html>"
        csp = page.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "connect-src 'self'" in csp
        assert "img-src 'self' data: blob:" in csp and "frame-ancestors 'none'" in csp
        assert page.headers["x-frame-options"] == "DENY"
        assert (await http.get("/assets/app.js")).headers["x-frame-options"] == "DENY"
        assert (await http.get("/assets/app.js")).text == "console.log(1)"
        assert (await http.get("/tasks")).status_code == 401
        assert (await http.get("/nothing-here")).status_code == 404


async def test_pending_rereads_only_the_logs_that_moved(
        local: tuple[LocalApp, ScriptedProvider]) -> None:
    app, _ = local
    task_id = await app.client.create("main", None, config={"interactive": False})
    reads: list[str] = []
    events = app.client.events

    async def counted(tid: str, after: int = 0) -> list[Any]:
        reads.append(tid)
        return await events(tid, after)

    app.client.events = counted  # type: ignore[method-assign]
    cache: dict[str, tuple[int, list[Pending]]] = {}
    assert await pending_tree(app.client, task_id, cache) == []
    assert await pending_tree(app.client, task_id, cache) == []
    assert reads == [task_id]
    await app.client.signal(task_id, "rename", {"title": "x"})

    async def moved() -> bool:
        await pending_tree(app.client, task_id, cache)
        return len(reads) > 1

    await _until(moved)


async def test_files_answer_an_unknown_task_404_and_a_bad_limit_400(
        local: tuple[LocalApp, ScriptedProvider], tmp_path: Path) -> None:
    app, _ = local
    api = create_app(app.client, token="secret", app=app, cwd=str(tmp_path))
    async with _http(api) as http:
        assert (await http.get("/files", params={"task_id": "nosuch"})).status_code == 404
        assert (await http.get("/files", params={"limit": "many"})).status_code == 400
