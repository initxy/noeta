"""``LocalApp`` passes what it is given on to its ``Runtime``; its own
defaults never collide with the caller's."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider


async def test_the_callers_poll_interval_wins(tmp_path: Path) -> None:
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                   ScriptedProvider([]), poll_interval=0.05)
    assert app.runtime.poll_interval == 0.05 and app.client.poll_interval == 0.05
    await app.store.close()
    default = LocalApp(SqliteStore(str(tmp_path / "u.db")), [], ScriptedProvider([]))
    assert default.runtime.poll_interval == 0.2 and default.client.poll_interval == 0.2
    await default.store.close()
