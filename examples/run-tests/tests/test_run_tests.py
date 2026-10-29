"""The run_tests example as a plain plugin file: it joins main by itself,
runs the test command in the task's exec env and nudges once after an edit."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from noeta import Agent, Runtime, ToolContext, function_tool
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import ToolSet
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]


@function_tool
def Edit(file_path: str, ctx: ToolContext) -> str:
    """Pretend to edit a file."""
    return f"edited {file_path}"


async def test_run_tests_runs_the_command_and_nudges_once_after_an_edit(tmp_path: Path) -> None:
    registry.load_plugin_files([HERE])
    plugin_cls = registry.load(registry.PLUGINS, "run_tests")
    assert plugin_cls.add_to == ("main",)
    plugin = plugin_cls(command=f"{sys.executable} -c \"print('3 passed')\"")
    agent = Agent(name="main", model="m", extensions=(plugin, ToolSet([Edit])))
    provider = ScriptedProvider([reply("", ("RunTests", {})),
                                 reply("", ("Edit", {"file_path": "x"})),
                                 reply("checking"), reply("again")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    client = Client(store)
    tid = await client.create("main", "fix it")
    await rt.run_until_idle()
    [ran, _edit] = [r.event.result for r in await client.events(tid)
                    if isinstance(r.event, ToolFinished)]
    assert "exit 0" in ran.text and "3 passed" in ran.text

    def nudges(request: Any) -> int:
        return sum("Files changed since the tests last ran" in m.text for m in request.messages)

    assert [nudges(r) for r in provider.requests] == [0, 0, 1]
    await client.send(tid, "and now?")
    await rt.run_until_idle()
    assert nudges(provider.requests[-1]) == 1  # recorded once, never repeated
    await store.close()


async def _one_run(tmp_path: Path, **config: Any) -> Any:
    """The result of one RunTests call made with the plugin's ``config``."""
    registry.load_plugin_files([HERE])
    plugin = registry.load(registry.PLUGINS, "run_tests")(**config)
    agent = Agent(name="main", model="m", extensions=(plugin,))
    provider = ScriptedProvider([reply("", ("RunTests", {})), reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    client = Client(store)
    tid = await client.create("main", "test it")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(tid)
                if isinstance(r.event, ToolFinished)]
    await store.close()
    return result


async def test_failing_tests_are_an_error_result(tmp_path: Path) -> None:
    result = await _one_run(
        tmp_path, command=f"{sys.executable} -c \"print('1 failed'); raise SystemExit(1)\"")
    assert result.is_error
    assert "exit 1" in result.text and "1 failed" in result.text


async def test_a_run_past_its_timeout_is_an_error_result(tmp_path: Path) -> None:
    result = await _one_run(
        tmp_path, command=f"{sys.executable} -c \"import time; time.sleep(30)\"", timeout=0.5)
    assert result.is_error
    assert "timed out" in result.text
