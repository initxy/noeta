"""Every built-in host gives tools a spill directory, so output too long to
return inline is saved and the model gets its path, as the tools promise."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli import home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.core.agent import Agent
from noeta.core.events import ToolFinished
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.host.local import LocalApp, default_spill_root
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.tools import limit_output


class Loud:
    name = "Loud"
    description = "Print a lot."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output(await limit_output("x" * 50_000, 1_000, ctx))


async def test_local_app_saves_long_output(tmp_path: Path) -> None:
    provider = ScriptedProvider([reply("", ("Loud", {})), reply("done")])
    agent = Agent(name="main", model="m", tools=[Loud()])
    async with LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                        env_for=lambda _s: LocalExecEnv(str(tmp_path)),
                        spill_root=str(tmp_path / "spill")) as app:
        tid = await app.client.create("main", "go")
        await app.client.wait(tid, timeout=10)
        [result] = [r.event.result for r in await app.client.events(tid)
                    if isinstance(r.event, ToolFinished)]
    saved = tmp_path / "spill" / tid
    [file] = list(saved.iterdir())
    assert file.read_text() == "x" * 50_000
    assert str(file) in result.text


async def test_every_host_has_a_spill_root(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [], ScriptedProvider([]))
    assert app.runtime.spill_root == default_spill_root()
    await app.store.close()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    (home / "config.toml").write_text(
        f'model = "m"\napi_key = "k"\nstore = "sqlite:///{tmp_path / "u.db"}"\n')
    cli = from_settings(load_settings(), cwd=str(tmp_path))
    assert cli.runtime.spill_root == str(home / "tool-results")
    await cli.store.close()
