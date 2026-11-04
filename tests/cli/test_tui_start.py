"""What the ``noeta`` command hands the terminal UI on this machine: the
effort asked for, and in a project not yet trusted a way to trust it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import noeta.cli.main as cli
from noeta.cli import config, home_dir
from noeta.cli.trust import project_trusted, untrusted_notice


class Built:
    """What ``from_settings`` returns, as far as the command touches it."""

    main_agent = "main"

    def __init__(self) -> None:
        self.notices: list[str] = []
        self.reloaded = 0

    def reload_plugins(self) -> list[str]:
        self.reloaded += 1
        return []


@pytest.fixture
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("NOETA_MODEL", "claude-opus-5-5")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    seen: dict[str, Any] = {"app": Built()}
    monkeypatch.setattr(config, "from_settings", lambda _s, **_kw: seen["app"])
    monkeypatch.setattr(cli, "front_end",
                        lambda _name: lambda app, start: seen.update(start=start))
    return seen


def test_an_untrusted_project_is_trusted_through_the_front_end(started: dict[str, Any]) -> None:
    cli.main(["--effort", "high"])
    start, app = started["start"], started["app"]
    assert start.task_config["effort"] == "high"
    (Path(start.cwd) / ".noeta" / "agents").mkdir(parents=True)
    stale = untrusted_notice(start.cwd, ".noeta/agents")
    app.notices = [stale, "something else"]
    start.trust()
    assert project_trusted(start.cwd) and app.reloaded == 1
    assert app.notices == ["something else"]
    cli.main([])
    assert started["start"].trust is None and "effort" not in started["start"].task_config


def test_continue_is_handed_to_the_front_end(started: dict[str, Any]) -> None:
    cli.main(["-c"])
    assert started["start"].continue_last and started["start"].resume is None
    cli.main([])
    assert not started["start"].continue_last
