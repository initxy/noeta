"""``noeta worker``: its options reach the runtime it serves with."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import main as cli


class _App:
    async def run_until(self, stop: asyncio.Event) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # the way an operator stops it
        await stop.wait()


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



def test_a_worker_sweeps_saved_outputs_once(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that only runs tasks removes ended tasks' old saved tool
    outputs as ``noeta serve`` does: one sweep loop, on the app's spill root."""
    from noeta.backends.stores.sqlite import SqliteStore
    from noeta.host import spill_sweep
    from noeta.host.local import LocalApp
    from noeta.testing import ScriptedProvider

    swept: list[str] = []

    async def sweeping(_store: Any, root: str, stop: asyncio.Event) -> None:
        swept.append(root)
        os.kill(os.getpid(), signal.SIGTERM)
        await stop.wait()

    def app(_settings: Any, **_kw: Any) -> LocalApp:
        return LocalApp(SqliteStore(str(tmp_path / "t.db")), [], ScriptedProvider([]),
                        spill_root=str(tmp_path / "spill"))

    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spill_sweep, "keep_spills_swept", sweeping)
    monkeypatch.setattr(cli, "_app", app)
    cli.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "worker"])
    assert swept == [str(tmp_path / "spill")]
