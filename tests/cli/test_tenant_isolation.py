"""Tenants' tools run as the server's OS user unless something stands between
them: ``noeta serve`` and ``noeta worker`` refuse to start with tenants
besides the default one unless the sandbox confines every command or the
operator accepts tenants separated at the API only."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from noeta.cli import config, home_dir, main, tenants


class _Started(Exception):
    """The command got past its checks and made its app."""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(home_dir, "HOME", path)
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
    _workspace(home, tmp_path, "acme")
    with pytest.raises(SystemExit, match=r"acme.*shared_os_user_tenants"):
        _run(tmp_path, *command)
    (home / "config.toml").write_text("[server]\nshared_os_user_tenants = true\n")
    with pytest.raises(_Started):
        _run(tmp_path, *command)


def _workspace(home: Path, tmp_path: Path, tenant: str) -> None:
    (tmp_path / f"{tenant}-ws").mkdir()
    (home / "tenants" / tenant).mkdir(parents=True)
    (home / "tenants" / tenant / "config.toml").write_text(
        f'workspace = "{tmp_path / f"{tenant}-ws"}"\n')


@pytest.mark.parametrize("known", ["token", "directory"])
def test_a_server_refuses_a_tenant_without_a_workspace(home: Path, tmp_path: Path,
                                                       known: str) -> None:
    (home / "config.toml").write_text("[server]\nshared_os_user_tenants = true\n")
    _workspace(home, tmp_path, "acme")
    if known == "token":
        tenants.add_token("beta")
    else:
        (home / "tenants" / "beta").mkdir()
        (home / "tenants" / "beta" / "config.toml").write_text("max_running = 2\n")
    with pytest.raises(SystemExit, match=r"beta have no workspace.*tenants/beta/config"):
        _run(tmp_path, "serve", "--no-worker")
    (home / "tenants" / "beta").mkdir(exist_ok=True)
    (home / "tenants" / "beta" / "config.toml").write_text(f'workspace = "{tmp_path}"\n')
    with pytest.raises(_Started):
        _run(tmp_path, "serve", "--no-worker")


@pytest.mark.parametrize("command", [("serve",), ("worker",)])
def test_a_server_directory_that_cannot_be_hidden_is_refused(
        home: Path, tmp_path: Path, command: tuple[str, ...]) -> None:
    (home / "config.toml").write_text(
        '[server]\nshared_os_user_tenants = true\ndirs = ["/var"]\n')
    _workspace(home, tmp_path, "acme")
    with pytest.raises(SystemExit, match=r"/var.*cannot be hidden.*tenant"):
        _run(tmp_path, *command)
    # Without other tenants it hides nothing from anyone: it starts.
    (home / "tenants" / "acme" / "config.toml").unlink()
    (home / "tenants" / "acme").rmdir()
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
    # The noeta command cannot set up a remote exec env; the way out it
    # names is one an operator can take.
    assert "run the workers with a remote exec env" not in problem
    box["allowUnsandboxedCommands"] = False
    (home / "settings.json").write_text(json.dumps({"sandbox": box}))
    assert tenants.isolation_problem(settings, str(tmp_path)) is None


def test_tokens_of_other_tenants_find_nothing_when_not_isolated(home: Path) -> None:
    acme = tenants.add_token("acme")
    mine = tenants.add_token("default")
    closed = tenants.tenant_tokens(others=False)
    assert closed(acme) is None and closed(mine) == "default"
    assert tenants.tenant_tokens()(acme) == "acme"


def test_a_worker_fences_the_tenants_workspaces(home: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "config.toml").write_text("[server]\nshared_os_user_tenants = true\n")
    (tmp_path / "acme-ws").mkdir()
    (home / "tenants" / "acme").mkdir(parents=True)
    (home / "tenants" / "acme" / "config.toml").write_text(
        f'workspace = "{tmp_path / "acme-ws"}"\n')
    seen: dict[str, object] = {}

    def started(*_a: object, **kw: object) -> None:
        seen.update(kw)
        raise _Started

    monkeypatch.setattr(main, "_app", started)
    with pytest.raises(_Started):
        _run(tmp_path, "worker")
    assert seen["tenant_workspaces"] == {"acme": str((tmp_path / "acme-ws").resolve())}


@pytest.mark.parametrize("command", [("serve",), ("worker",)])
def test_a_worker_that_knows_no_tenants_still_refuses_their_tasks(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        command: tuple[str, ...]) -> None:
    seen: dict[str, object] = {}

    def started(*_a: object, **kw: object) -> None:
        seen.update(kw)
        raise _Started

    monkeypatch.setattr(main, "_app", started)
    with pytest.raises(_Started):
        _run(tmp_path, *command)
    refusal = (seen.get("permissions") or {}).get("tenant_refusal")  # type: ignore[attr-defined]
    assert refusal and "shared_os_user_tenants" in refusal
    (home / "config.toml").write_text("[server]\nshared_os_user_tenants = true\n")
    seen.clear()
    with pytest.raises(_Started):
        _run(tmp_path, *command)
    assert "tenant_refusal" not in (seen.get("permissions") or {})  # type: ignore[operator]


async def test_the_permissions_plugin_gets_the_fenced_places(home: Path,
                                                             tmp_path: Path) -> None:
    settings = config.load_settings(model="sonnet", store=f"sqlite:///{tmp_path / 't.db'}")
    settings = dataclasses.replace(settings, api_key="k")
    (home / "settings.json").write_text(json.dumps(
        {"sandbox": {"filesystem": {"denyRead": ["~/secrets", "/data/private"]}}}))
    app = config.from_settings(settings, cwd=str(tmp_path),
                               tenant_workspaces={"acme": "/srv/acme"})
    perms = next(e for e in app.runtime.agents["main"].extensions if e.name == "permissions")
    fence = perms.fence  # type: ignore[attr-defined]
    assert fence.workspaces == {"acme": "/srv/acme"}
    # The operator's denyRead holds for a tenant's file tools too.
    assert fence.deny_read == (str(Path("~/secrets").expanduser()), "/data/private")
    assert fence.store_files[0] == str(tmp_path / "t.db")
    assert fence.spill_root == str(home / "tool-results")
    await app.store.close()


def test_the_macos_sandbox_is_not_a_tenant_boundary(home: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    settings = config.Settings(model="m")
    box = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
    (home / "settings.json").write_text(json.dumps({"sandbox": box}))
    assert tenants.isolation_problem(settings, str(tmp_path)) is None
    monkeypatch.setattr(tenants.sys, "platform", "darwin")
    problem = tenants.isolation_problem(settings, str(tmp_path))
    assert problem is not None and "macOS" in problem and "shared_os_user_tenants" in problem
    shared = dataclasses.replace(settings, server={"shared_os_user_tenants": True})
    assert tenants.isolation_problem(shared, str(tmp_path)) is None


def test_a_macos_server_with_a_confining_sandbox_refuses_tenants(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    box = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
    (home / "settings.json").write_text(json.dumps({"sandbox": box}))
    with pytest.raises(_Started):
        _run(tmp_path, "worker")
    monkeypatch.setattr(tenants.sys, "platform", "darwin")
    # The default tenant alone starts as before.
    with pytest.raises(_Started):
        _run(tmp_path, "worker")
    _workspace(home, tmp_path, "acme")
    with pytest.raises(SystemExit, match=r"acme.*macOS"):
        _run(tmp_path, "worker")
