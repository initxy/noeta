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
