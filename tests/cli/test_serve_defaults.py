"""``noeta serve``: the effort and permission mode given to it are what the
tasks its clients create start with, unless a request names its own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.cli import home_dir, main


class _Started(BaseException):  # not an Exception: past main()'s crash report
    pass


class _Client:
    def __init__(self) -> None:
        self.task_config: dict[str, Any] = {"prompts": {}}


class _App:
    def __init__(self) -> None:
        self.client = _Client()

    async def start(self, **_kw: Any) -> None:
        raise _Started


def test_serves_tasks_with_the_effort_and_mode_asked(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.setenv("NOETA_EFFORT", "max")
    monkeypatch.chdir(tmp_path)
    made: list[_App] = []

    def app(_s: Any, **_kw: Any) -> _App:
        made.append(_App())
        return made[-1]

    monkeypatch.setattr(main, "_app", app)
    argv = ["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db"]
    with pytest.raises(_Started):
        main.main([*argv, "--permission-mode", "plan", "serve", "--no-token"])
    assert made[-1].client.task_config == {"prompts": {}, "effort": "max",
                                           "permission_mode": "plan"}
    with pytest.raises(SystemExit, match="needs --allow-bypass-permissions"):
        main.main([*argv, "--permission-mode", "bypassPermissions", "serve", "--no-token"])
    with pytest.raises(_Started):
        main.main([*argv, "--permission-mode", "bypassPermissions",
                   "serve", "--no-token", "--allow-bypass-permissions"])
    assert made[-1].client.task_config["permission_mode"] == "bypassPermissions"


@pytest.mark.parametrize(("argv", "bundled", "served"), [
    ([], "/pkg/web_dist", "/pkg/web_dist"),
    ([], None, None),
    (["--web", "/my/dist"], "/pkg/web_dist", "/my/dist"),
])
def test_serve_serves_the_bundled_web_app_unless_told_another(
        argv: list[str], bundled: str | None, served: str | None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import types

    import noeta.host.http as http

    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    seen: dict[str, Any] = {}

    async def started(**_kw: Any) -> None:
        return None

    fake = types.SimpleNamespace(client=_Client(), start=started, deltas=None,
                                 runtime=types.SimpleNamespace(agents={}), plugin_admin=None)
    monkeypatch.setattr(main, "_app", lambda _s, **_kw: fake)
    monkeypatch.setattr(http, "bundled_web_dir", lambda: bundled)

    def create_app(_client: Any, **kw: Any) -> None:
        seen.update(kw)
        raise _Started

    monkeypatch.setattr(http, "create_app", create_app)
    with pytest.raises(_Started):
        main.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db",
                   "serve", "--no-token", *argv])
    assert seen["web_dir"] == served
    assert seen["projects_file"] == str(tmp_path / "projects.json")


def test_the_bundled_web_app_is_found_beside_the_module(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.http as http

    monkeypatch.setattr(http, "__file__", str(tmp_path / "http.py"))
    assert http.bundled_web_dir() is None
    (tmp_path / "web_dist").mkdir()
    assert http.bundled_web_dir() is None
    (tmp_path / "web_dist" / "index.html").write_text("<html></html>")
    assert http.bundled_web_dir() == str(tmp_path / "web_dist")
