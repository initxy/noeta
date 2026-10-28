"""Named prompts and their overrides: host directory < workspace directory <
task config; recorded when the task first starts, so later edits do not
reach a running task."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.prompts import resolve
from noeta.core.runner import Runtime
from noeta.host.local import LocalClient
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


def test_resolve() -> None:
    assert resolve({}, "x", "base\n") == "base\n"
    assert resolve({"x": "new\n"}, "x", "base\n") == "new\n"
    assert resolve({"x.append": "more\n"}, "x", "base\n") == "base\n\nmore\n"
    assert resolve({"x.append": "more"}, "x", "base") == "base\n\nmore"
    assert resolve({"x": "", "x.append": "only"}, "x", "base") == "only"
    assert resolve({"x.append": "\n"}, "x", "base") == "base"


def _runtime(store: SqliteStore, provider: ScriptedProvider, work: Path,
             host_dir: Path) -> Runtime:
    agent = Agent(name="a", model="m", system_prompt="You help.\n", extensions=[FileTools()])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep,
                   prompt_dirs=[str(host_dir)])


async def test_overrides_layer_and_are_fixed_at_start(tmp_path: Path) -> None:
    work, host = tmp_path / "w", tmp_path / "host"
    (work / ".noeta" / "prompts" / "tool").mkdir(parents=True)
    (host / "system").mkdir(parents=True)
    (host / "system" / "a.append.md").write_text("From the host.\n")
    (host / "tool").mkdir()
    (host / "tool" / "Write.md").write_text("host write\n")
    (work / ".noeta" / "prompts" / "tool" / "Write.md").write_text("workspace write\n")
    (work / ".noeta" / "prompts" / "tool" / "Read.md").write_text("Read a file.\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one"), reply("two")])
    rt = _runtime(store, provider, work, host)
    client = Client(store)
    tid = await client.create("a", "hi", config={"prompts": {"tool/Edit": "config edit"}})
    await rt.run_until_idle()

    first = provider.requests[0]
    assert first.system[0].text == "You help.\n\nFrom the host.\n"
    tools = {t.name: t.description for t in first.tools}
    assert tools["Read"] == "Read a file.\n"
    assert tools["Write"] == "workspace write\n"  # the workspace wins over the host
    assert tools["Edit"] == "config edit"  # the task config wins over both

    # Edited after the task started: the running task keeps what it recorded.
    (work / ".noeta" / "prompts" / "tool" / "Read.md").write_text("changed\n")
    (host / "system" / "a.append.md").unlink()
    await client.send(tid, "again")
    await rt.run_until_idle()
    second = provider.requests[1]
    assert second.system[0].text == first.system[0].text
    assert {t.name: t.description for t in second.tools} == tools
    await store.close()


async def test_an_untrusted_workspace_changes_no_prompt(tmp_path: Path) -> None:
    work, host = tmp_path / "w", tmp_path / "host"
    (work / ".noeta" / "prompts" / "tool").mkdir(parents=True)
    (work / ".noeta" / "prompts" / "tool" / "Read.md").write_text("workspace read\n")
    (host / "tool").mkdir(parents=True)
    (host / "tool" / "Write.md").write_text("host write\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    rt = _runtime(store, provider, work, host)
    seen: list[str] = []
    rt.workspace_trusted = lambda cwd: bool(seen.append(cwd))
    await Client(store).create("a", "hi", config={"cwd": str(work)})
    await rt.run_until_idle()
    tools = {t.name: t.description for t in provider.requests[0].tools}
    assert tools["Write"] == "host write\n"
    assert tools["Read"] != "workspace read\n"
    assert seen == [str(work)]
    await store.close()


async def test_no_override_files_leave_the_defaults(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    rt = _runtime(store, provider, work, tmp_path / "missing")
    await Client(store).create("a", "hi")
    await rt.run_until_idle()
    assert provider.requests[0].system[0].text == "You help.\n"
    await store.close()


async def test_front_end_prompts_reach_every_task(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("ok")])
    rt = _runtime(store, provider, work, tmp_path / "missing")
    client = LocalClient(store, rt, task_config={"prompts": {"system/a": "Replaced.\n"}})
    await client.create("a", "hi", config={"prompts": {"system/a.append": "Also.\n"}})
    await rt.run_until_idle()
    assert provider.requests[0].system[0].text == "Replaced.\n\nAlso.\n"
    await store.close()
