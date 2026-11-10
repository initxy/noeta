"""The projects the web app lists down its side (saved directories and the
directories of unarchived sessions, with their session counts) and the
folder picker's directory listing, of any directory."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("starlette")

import httpx

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.http import create_app
from noeta.host.local import LocalApp
from noeta.host.projects import SavedProjects, list_dirs, projects
from noeta.testing import ScriptedProvider

H = {"authorization": "Bearer secret"}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    for sub in ("alpha", "Beta", "gamma/inner", ".hidden"):
        (root / sub).mkdir(parents=True)
    (root / "a-file.txt").write_text("x")
    (tmp_path / "outside").mkdir()
    return root


def _api(tmp_path: Path, root: Path, **kw: str) -> tuple[LocalApp, httpx.AsyncClient, Path]:
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                   ScriptedProvider(), env_for=lambda _s: None)
    saved = tmp_path / "projects.json"
    api = create_app(app.client, token="secret", app=app, cwd=str(root),
                     projects_file=str(saved), **kw)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api),  # type: ignore[arg-type]
                             base_url="http://127.0.0.1", headers=H, timeout=10)
    return app, http, saved


async def test_projects_are_saved_ones_then_session_directories(
        tmp_path: Path, tree: Path) -> None:
    app, http, saved = _api(tmp_path, tree)
    alpha, beta, gamma = (str(tree / n) for n in ("alpha", "Beta", "gamma"))
    for where in (alpha, alpha, gamma):
        await app.client.create("main", None, config={"cwd": where, "interactive": False})
    gone = await app.client.create("main", None, config={"cwd": beta, "interactive": False})
    async with http:
        await http.post(f"/tasks/{gone}/signals",
                        json={"name": "archive", "data": {"archived": True}, "wake": False})
        assert (await http.get("/projects")).json() == [
            {"path": gamma, "name": "gamma", "saved": False, "sessions": 1},
            {"path": alpha, "name": "alpha", "saved": False, "sessions": 2}]

        added = await http.post("/projects", json={"path": beta})
        assert added.status_code == 200
        assert added.json() == {"path": beta, "name": "Beta", "saved": True, "sessions": 0}
        # Saving again, or by a relative path, keeps one entry.
        await http.post("/projects", json={"path": "alpha"})
        await http.post("/projects", json={"path": alpha + "/"})
        listed = (await http.get("/projects")).json()
        assert [(p["path"], p["saved"], p["sessions"]) for p in listed] == [
            (beta, True, 0), (alpha, True, 2), (gamma, False, 1)]
        assert json.loads(saved.read_text())["projects"] == [beta, alpha]
        assert saved.stat().st_mode & 0o777 == 0o600

        gone_ok = await http.request("DELETE", "/projects", json={"path": alpha})
        assert gone_ok.json() == {"ok": True}
        listed = (await http.get("/projects")).json()
        assert [(p["path"], p["saved"]) for p in listed] == [
            (beta, True), (gamma, False), (alpha, False)]


async def test_a_project_is_any_directory_and_nothing_else(
        tmp_path: Path, tree: Path) -> None:
    _, http, saved = _api(tmp_path, tree)
    async with http:
        missing = await http.post("/projects", json={"path": str(tree / "nope")})
        assert missing.status_code == 400 and "does not exist" in missing.json()["error"]
        a_file = await http.post("/projects", json={"path": str(tree / "a-file.txt")})
        assert a_file.status_code == 400 and "not a directory" in a_file.json()["error"]
        assert (await http.post("/projects", json={})).status_code == 400
        assert (await http.post("/projects", json={"path": 3})).status_code == 400
        unauthorized = await http.get("/projects", headers={"authorization": "Bearer no"})
        assert unauthorized.status_code == 401
    assert not saved.exists()


async def test_a_project_outside_the_servers_directory_is_saved(
        tmp_path: Path, tree: Path) -> None:
    _, http, saved = _api(tmp_path, tree)
    async with http:
        outside = str(tmp_path / "outside")
        added = await http.post("/projects", json={"path": "../outside"})
        assert added.status_code == 200 and added.json()["path"] == outside
    assert json.loads(saved.read_text())["projects"] == [outside]


async def test_dirs_lists_the_subdirectories_of_any_directory(
        tmp_path: Path, tree: Path) -> None:
    _, http, _ = _api(tmp_path, tree)
    async with http:
        top = (await http.get("/dirs")).json()
        assert top == {"path": str(tree), "parent": str(tmp_path), "dirs": [
            {"name": "alpha", "path": str(tree / "alpha")},
            {"name": "Beta", "path": str(tree / "Beta")},
            {"name": "gamma", "path": str(tree / "gamma")}]}
        inner = (await http.get("/dirs", params={"path": str(tree / "gamma")})).json()
        assert inner == {"path": str(tree / "gamma"), "parent": str(tree),
                         "dirs": [{"name": "inner", "path": str(tree / "gamma" / "inner")}]}
        assert (await http.get("/dirs", params={"path": "gamma"})).json() == inner
        up = (await http.get("/dirs", params={"path": str(tmp_path)})).json()
        assert {"name": "outside", "path": str(tmp_path / "outside")} in up["dirs"]
        assert (await http.get("/dirs", params={"path": "/"})).json()["parent"] is None
        assert (await http.get("/dirs", params={"path": str(tree / "a-file.txt")})
                ).status_code == 400
        assert (await http.get("/dirs", params={"path": str(tree / "nope")})).status_code == 400
        spec = (await http.get("/openapi.json")).json()["paths"]
        assert set(spec["/projects"]) == {"get", "post", "delete"} and "get" in spec["/dirs"]


def test_saved_projects_in_memory_and_a_broken_file(tmp_path: Path) -> None:
    memory = SavedProjects()
    memory.add("/a")
    memory.add("/a")
    assert memory.paths() == ["/a"]
    memory.remove("/a")
    assert memory.paths() == []
    broken = tmp_path / "projects.json"
    broken.write_text("{not json")
    assert SavedProjects(broken).paths() == []
    broken.write_text(json.dumps({"projects": ["/x", 3, ""]}))
    assert SavedProjects(broken).paths() == ["/x"]
    broken.write_text("[]")
    assert SavedProjects(broken).paths() == []


def test_projects_compare_directories_by_their_real_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    got = projects([str(tmp_path / "link")], [{"cwd": str(real)}, {"cwd": ""},
                                              {"cwd": str(tmp_path / "link")}])
    assert got == [{"path": str(real), "name": "real", "saved": True, "sessions": 2}]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_directory_is_a_value_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        with pytest.raises(ValueError, match="cannot read"):
            list_dirs(str(locked))
    finally:
        locked.chmod(0o700)


def test_the_saved_list_lives_beside_a_sqlite_store_else_in_noetas_home(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli import home_dir

    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    assert home_dir.projects_file(f"sqlite:///{tmp_path}/s/t.db") == tmp_path / "s" / "projects.json"
    assert home_dir.projects_file("postgres://db/x") == tmp_path / "home" / "projects.json"
