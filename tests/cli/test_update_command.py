"""``noeta update``: a uv tool install is reinstalled with every extra and
``--with`` its receipt names (noeta's pin dropped), a pip install upgraded
with the extras installed, a source install left alone; nothing runs when
PyPI has nothing newer or cannot be reached."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import noeta.cli.main as cli
import noeta.host.stream_json as stream_json
from noeta.cli import service_command, update_command

RECEIPT = """\
[tool]
requirements = [
    { name = "noeta", extras = ["tui", "server"], specifier = "==1.1.0" },
    { name = "socksio" },
    { name = "rich", extras = ["jupyter"], specifier = ">=13" },
    { name = "mytool", git = "https://example.com/mytool.git" },
]
python = "3.12"
entrypoints = [{ name = "noeta", install-path = "/home/u/.local/bin/noeta", from = "noeta" }]

[tool.options]
index = [{ url = "https://pypi.org/simple", explicit = false, default = true }]
"""


class Ran:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        out = "1.2.0 (noeta)\n" if cmd[-1] == "--version" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


@pytest.fixture
def ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ran:
    """A uv tool install of noeta 1.1.0 (its receipt in ``sys.prefix``),
    PyPI's latest 1.2.0, and every command recorded instead of run."""
    (tmp_path / "uv-receipt.toml").write_text(RECEIPT)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(update_command, "direct_url", lambda: None)
    monkeypatch.setattr(update_command, "installed_extras", lambda: ["server", "tui"])
    monkeypatch.setattr(update_command, "latest_version", lambda: "1.2.0")
    monkeypatch.setattr(stream_json, "noeta_version", lambda: "1.1.0")
    monkeypatch.setattr(update_command.shutil, "which",
                        lambda name: f"/bin/{name}" if name == "uv" else None)
    monkeypatch.setattr(service_command, "restart_if_installed", lambda: None)
    recorded = Ran()
    monkeypatch.setattr(update_command.subprocess, "run", recorded)
    return recorded


def _update() -> int:
    with pytest.raises(SystemExit) as done:
        cli.main(["update"])
    return int(done.value.code or 0)


def test_a_uv_tool_install_is_reinstalled_with_its_extras_and_with(
        ran: Ran, capsys: pytest.CaptureFixture[str]) -> None:
    assert _update() == 0
    assert ran.commands[0] == [
        "/bin/uv", "tool", "install", "--reinstall", "noeta[tui,server]",
        "--with", "socksio", "--with", "rich[jupyter]>=13",
        "--with", "mytool @ git+https://example.com/mytool.git",
        "--python", "3.12", "--default-index", "https://pypi.org/simple"]
    assert ran.commands[1] == [sys.executable, "-m", "noeta.cli.main", "--version"]
    out = capsys.readouterr().out
    assert "noeta update: 1.1.0 -> 1.2.0" in out and "noeta update: now at 1.2.0" in out


def test_an_installed_service_is_restarted_after_the_upgrade(
        ran: Ran, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    restarted: list[bool] = []
    monkeypatch.setattr(service_command, "restart_if_installed",
                        lambda: restarted.append(True) or True)
    assert _update() == 0
    assert restarted == [True] and ran.commands[0][:3] == ["/bin/uv", "tool", "install"]
    assert "restarted noeta service" in capsys.readouterr().out
    monkeypatch.setattr(service_command, "restart_if_installed", lambda: False)
    assert _update() == 0
    assert "noeta service restart" in capsys.readouterr().err


def test_up_to_date_runs_nothing(ran: Ran, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(update_command, "latest_version", lambda: "1.1.0")
    assert _update() == 0
    assert ran.commands == []
    assert capsys.readouterr().out == "noeta is up to date (1.1.0)\n"


def test_pypi_out_of_reach_is_one_line_and_runs_nothing(
        ran: Ran, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    def down() -> str:
        raise httpx.ConnectError("Name or service not known")

    monkeypatch.setattr(update_command, "latest_version", down)
    assert _update() == 1
    assert ran.commands == []
    err = capsys.readouterr().err
    assert err == ("noeta update: cannot reach PyPI to check for a new version "
                   "(Name or service not known); nothing changed\n")


def test_a_failed_update_says_so(ran: Ran, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(update_command.subprocess, "run",
                        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 2))
    assert _update() == 2
    assert "the update failed (exit 2)" in capsys.readouterr().err


def test_a_source_install_changes_nothing(ran: Ran, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(update_command, "direct_url", lambda: {
        "url": "file:///src/noeta", "dir_info": {"editable": True}})
    assert _update() == 0
    assert ran.commands == []
    out = capsys.readouterr().out
    assert "runs from source (/src/noeta)" in out and "nothing is changed" in out
    assert "uv tool install --reinstall 'noeta[server,tui]'" in out


def test_a_pip_install_is_upgraded_with_its_installed_extras(
        ran: Ran, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv-receipt.toml").unlink()
    monkeypatch.setattr(update_command.importlib.util, "find_spec", lambda _name: object())
    assert _update() == 0
    assert ran.commands[0] == [sys.executable, "-m", "pip", "install", "-U",
                               "noeta[server,tui]"]
    # An environment without pip (made by uv) is upgraded with uv pip.
    monkeypatch.setattr(update_command.importlib.util, "find_spec", lambda _name: None)
    ran.commands.clear()
    assert _update() == 0
    assert ran.commands[0] == ["/bin/uv", "pip", "install", "--python", sys.executable, "-U",
                               "noeta[server,tui]"]


def test_running_the_command_fails_cleanly(ran: Ran, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    def missing(cmd: list[str], **_kw: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(update_command.subprocess, "run", missing)
    assert _update() == 1
    assert "cannot run /bin/uv: No such file or directory" in capsys.readouterr().err


@respx.mock
def test_the_latest_version_is_pypis() -> None:
    route = respx.get(update_command.PYPI_JSON)
    route.respond(json={"info": {"version": "3.4.5"}})
    assert update_command.latest_version() == "3.4.5"
    route.respond(json={"info": {}})
    with pytest.raises(ValueError):
        update_command.latest_version()
    route.respond(503)
    with pytest.raises(httpx.HTTPStatusError):
        update_command.latest_version()


def test_versions_compare_by_their_numbers() -> None:
    assert update_command._newer("1.10.0", "1.9.3")
    assert not update_command._newer("1.1.1", "1.1.1")
    assert not update_command._newer("1.1.0", "1.1.1")  # a checkout ahead of PyPI
    assert update_command._newer("1.2.0rc1", "1.1.0")


@pytest.mark.parametrize(("recorded", "source"), [
    (None, False),  # from an index
    ('{"url": "file:///d/noeta-1.1.0-py3-none-any.whl", "archive_info": {}}', False),
    ('{"url": "file:///src/noeta", "dir_info": {"editable": true}}', True),
    ('{"url": "file:///src/noeta", "dir_info": {}}', True),
    ('{"url": "https://github.com/initxy/noeta", "vcs_info": {"vcs": "git"}}', True),
    ("{broken", True),  # unreadable: changed nothing rather than guessed
])
def test_only_a_directory_or_a_repository_counts_as_source(
        recorded: str | None, source: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    class Dist:
        def read_text(self, _name: str) -> str | None:
            return recorded

    monkeypatch.setattr(update_command, "_distribution", lambda: Dist())
    assert (update_command.direct_url() is not None) == source


def test_what_this_environment_is_installed_from_and_with() -> None:
    # The test environment: an editable install with every extra.
    found = update_command.direct_url()
    assert found is not None and str(found.get("url", "")).startswith("file://")
    assert {"server", "tui"} <= set(update_command.installed_extras())
