"""Tenants' tools run as the server's OS user unless something stands between
them: ``noeta serve`` and ``noeta worker`` refuse to start with tenants
besides the default one unless the sandbox confines every command or the
operator accepts tenants separated at the API only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from noeta.cli import config, main, tenants


class _Started(Exception):
    """The command got past its checks and made its app."""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(config, "HOME", path)
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    def started(*_a: object, **_kw: object) -> None:
        raise _Started

    monkeypatch.setattr(main, "_app", started)
    return path


def _run(tmp_path: Path, *command: str) -> None:
    main.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", *command])


@pytest.mark.parametrize("command", [("serve",), ("worker",)])
def test_tenants_without_a_boundary_are_refused(home: Path, tmp_path: Path,
                                                command: tuple[str, ...]) -> None:
    tenants.add_token("acme")
    with pytest.raises(SystemExit, match=r"acme.*shared_os_user_tenants"):
        _run(tmp_path, *command)
    (home / "config.toml").write_text("[server]\nshared_os_user_tenants = true\n")
    with pytest.raises(_Started):
        _run(tmp_path, *command)


def test_a_server_without_tenants_starts(home: Path, tmp_path: Path) -> None:
    with pytest.raises(_Started):
        _run(tmp_path, "serve")


def test_a_sandbox_that_confines_every_command_is_a_boundary(home: Path,
                                                             tmp_path: Path) -> None:
    settings = config.Settings(model="m")
    box = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": True}
    (home / "settings.json").write_text(json.dumps({"sandbox": box}))
    problem = tenants.isolation_problem(settings, str(tmp_path))
    assert problem is not None and "allowUnsandboxedCommands: false" in problem
    assert "sandbox.enabled" not in problem
    box["allowUnsandboxedCommands"] = False
    (home / "settings.json").write_text(json.dumps({"sandbox": box}))
    assert tenants.isolation_problem(settings, str(tmp_path)) is None


def test_tokens_of_other_tenants_find_nothing_when_not_isolated(home: Path) -> None:
    acme = tenants.add_token("acme")
    mine = tenants.add_token("default")
    closed = tenants.tenant_tokens(others=False)
    assert closed(acme) is None and closed(mine) == "default"
    assert tenants.tenant_tokens()(acme) == "acme"
