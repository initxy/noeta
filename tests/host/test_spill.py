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


async def test_the_model_reads_a_saved_output_without_asking(tmp_path: Path) -> None:
    """Claude Code reads its tool-result files without a prompt; so does a
    task here, outside its working directory, in default mode."""
    from noeta.plugins.fs import FileTools
    from noeta.plugins.permissions import Permissions

    work = tmp_path / "w"
    work.mkdir()
    spill = tmp_path / "spill"
    provider = ScriptedProvider([reply("", ("Loud", {})), reply("saved")])
    agent = Agent(name="main", model="m", tools=[Loud()], extensions=[
        FileTools(),
        Permissions(mode="default", allow=["Loud"], plans_dir=str(tmp_path / "plans"))])
    async with LocalApp(SqliteStore(str(tmp_path / "t.db")), [agent], provider,
                        env_for=lambda _s: LocalExecEnv(str(work)),
                        spill_root=str(spill)) as app:
        tid = await app.client.create("main", "go", config={"cwd": str(work)})
        await app.client.wait(tid, timeout=10)
        [file] = list((spill / tid).iterdir())
        provider.add(reply("", ("Read", {"file_path": str(file), "limit": 1})), reply("done"))
        await app.client.send(tid, "read it")
        row = await app.client.wait(tid, timeout=10)
        results = [r.event.result for r in await app.client.events(tid)
                   if isinstance(r.event, ToolFinished)]
    assert row.status == "waiting" and results[-1].call_id != results[0].call_id
    assert not results[-1].is_error and "xxxx" in results[-1].text


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


async def test_an_ended_tasks_old_saved_outputs_are_swept(tmp_path: Path) -> None:
    """Kept while the task is open, however old; removed once it ended and
    nothing was written there for 30 days (Claude Code's cleanup period)."""
    import os
    import time

    from noeta.core.client import Client
    from noeta.core.runner import Runtime
    from noeta.host.spill_sweep import SPILL_KEEP_DAYS, sweep_spills

    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("a"), reply("b"), reply("c")])
    agent = Agent(name="main", model="m")
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider)
    client = Client(store)
    open_old, ended_old, ended_new = [await client.create("main", "go") for _ in range(3)]
    await rt.run_until_idle()
    for tid in (ended_old, ended_new):
        await client.cancel(tid)
    await rt.run_until_idle()
    root = tmp_path / "spill"
    old = time.time() - (SPILL_KEEP_DAYS + 1) * 86_400
    for name in (open_old, ended_old, ended_new, "gone"):
        (root / name).mkdir(parents=True)
        (root / name / "c.txt").write_text("x")
        if name != ended_new:
            for p in (root / name / "c.txt", root / name):
                os.utime(p, (old, old))
    assert sorted(await sweep_spills(store, str(root))) == sorted([ended_old, "gone"])
    assert sorted(p.name for p in root.iterdir()) == sorted([open_old, ended_new])
    await store.close()
