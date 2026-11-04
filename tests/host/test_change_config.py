"""``change_config``: what a front end calls to change a task's model or
effort (``/model``, ``/effort``) without reaching into the kernel."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.app import change_config
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply


async def test_the_next_request_runs_at_the_changed_effort(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("one"), reply("two")])
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")),
                   [Agent(name="main", model="claude-opus-5-5")], provider,
                   env_for=lambda _s: None)
    task = await app.client.create("main", "hi")
    await app.runtime.run_until_idle()
    await change_config(app.client, task, {"effort": "low"})
    await app.client.send(task, "again")
    await app.runtime.run_until_idle()
    assert [r.effort for r in provider.requests] == [None, "low"]
