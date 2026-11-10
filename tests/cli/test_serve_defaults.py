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
