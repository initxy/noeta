"""Front ends attached to a ``noeta serve`` server: no local model needed,
and new tasks carry neither this machine's directory nor its model unless
asked (``--cwd`` names a directory on the server, ``--model`` a model)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import noeta.cli.main as cli
from noeta.cli import config, home_dir, server_token


@pytest.fixture(autouse=True)
def no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    for name in ("NOETA_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(("argv", "asked"), [
    ([], {}),
    (["--cwd", "projects/web", "--model", "opus"], {"cwd": "projects/web", "model": "opus"}),
])
def test_the_tui_on_a_server_needs_no_local_model(
        argv: list[str], asked: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.host.remote import RemoteApp

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli, "front_end",
                        lambda _name: lambda app, start: seen.update(app=app, start=start))
    monkeypatch.setattr(server_token, "server_problem", lambda *_a: None)
    cli.main(["--server", "http://api.internal:8642", *argv])
    assert isinstance(seen["app"], RemoteApp)
    assert str(seen["app"].client.http.base_url) == "http://api.internal:8642"
    assert seen["start"].on_server and seen["start"].task_config == asked


def test_the_tui_without_a_server_still_needs_a_model() -> None:
    with pytest.raises(SystemExit, match="no model configured"):
        cli.main([])


def test_print_mode_refuses_a_server_instead_of_running_here() -> None:
    with pytest.raises(SystemExit, match="-p runs its task here"):
        cli.main(["-p", "hi", "--server", "http://api.internal:8642"])


def test_acp_on_a_server_leaves_the_directory_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.acp as acp

    seen: dict[str, Any] = {}

    async def serve(_app: Any, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(acp, "serve_stdio", serve)
    monkeypatch.setattr(server_token, "server_problem", lambda _url, _token: None)
    cli.main(["acp", "--server", "http://api.internal:8642"])
    assert seen == {"model": "", "editor_cwd": False, "cwd": "", "config": {}}


@pytest.mark.parametrize("argv", [
    ["--cwd", "projects/web", "--model", "opus", "acp", "--server", "http://api.internal:8642"],
    ["acp", "--server", "http://api.internal:8642", "--cwd", "projects/web", "--model", "opus"],
    ["--cwd", "elsewhere", "acp", "--server", "http://api.internal:8642",
     "--cwd", "projects/web", "--model", "opus"],
])
def test_acp_takes_cwd_and_model_on_either_side(
        argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.acp as acp

    seen: dict[str, Any] = {}

    async def serve(_app: Any, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(acp, "serve_stdio", serve)
    monkeypatch.setattr(server_token, "server_problem", lambda _url, _token: None)
    cli.main(argv)
    assert seen == {"model": "opus", "editor_cwd": False, "cwd": "projects/web", "config": {}}


def test_acp_on_a_server_starts_sessions_with_the_effort_and_mode_asked(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import noeta.host.acp as acp

    seen: dict[str, Any] = {}

    async def serve(_app: Any, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(acp, "serve_stdio", serve)
    monkeypatch.setattr(server_token, "server_problem", lambda _url, _token: None)
    cli.main(["--effort", "high", "--permission-mode", "plan",
              "acp", "--server", "http://api.internal:8642"])
    assert seen["config"] == {"effort": "high", "permission_mode": "plan"}
