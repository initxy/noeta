"""``noeta worker``: its options reach the runtime it serves with."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import main as cli


class _Runtime:
    async def serve(self, stop: asyncio.Event) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # the way an operator stops it
        await stop.wait()


class _Store:
    async def close(self) -> None:
        return None


class _App:
    runtime = _Runtime()
    store = _Store()
    channels: tuple[Any, ...] = ()
    client = None


@pytest.mark.parametrize(("argv", "lease"), [([], None), (["--lease", "1.5"], 1.5)])
def test_worker_lease(argv: list[str], lease: float | None, tmp_path: Path,
                      monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def app(_settings: Any, **kw: Any) -> _App:
        seen.update(kw)
        return _App()

    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_app", app)
    cli.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db",
              "worker", "--concurrency", "3", *argv])
    assert seen["concurrency"] == 3 and seen.get("lease_seconds") == lease


def _worker_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        argv: list[str], config: str = "") -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def app(_settings: Any, **kw: Any) -> _App:
        seen.update(kw)
        return _App()

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(config)
    monkeypatch.setenv("NOETA_HOME", str(home))
    monkeypatch.setattr(cli.home_dir, "HOME", home)
    monkeypatch.setattr(cli, "_app", app)
    monkeypatch.chdir(tmp_path)
    cli.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "worker", *argv])
    permissions: dict[str, Any] = seen["permissions"]
    return permissions


@pytest.mark.parametrize(("argv", "disabled"), [([], True),
                                                (["--allow-bypass-permissions"], False)])
def test_a_worker_refuses_bypass_like_serve(argv: list[str], disabled: bool, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    permissions = _worker_permissions(tmp_path, monkeypatch, argv)
    assert permissions.get("disable_bypass_mode", False) is disabled

