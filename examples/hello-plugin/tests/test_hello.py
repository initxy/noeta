"""The example plugin, installed as a separate package: found through its
entry point (nothing patched), named in an agent file and in config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta import CommandContext, Runtime, parse_agent_file
from noeta.agents.agent_files import build_agents
from noeta.backends.stores.sqlite import SqliteStore
from noeta.cli import home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.commands import expand_command
from noeta.core.events import ExtensionRecorded, ToolFinished
from noeta.host.pending import pending
from noeta.host.questions import answer_question
from noeta.testing import ScriptedProvider, reply

AGENT = """---
name: greeter
plugins:
  - hello: {greeting: Hi}
---
You greet people.
"""


def test_the_entry_point_is_found() -> None:
    assert "hello" in registry.available(registry.PLUGINS)


async def test_an_agent_file_runs_the_plugin(tmp_path: Path) -> None:
    [agent] = build_agents([parse_agent_file(AGENT)], model="m")
    names = ["Ada", "Bob", "Cy"]
    provider = ScriptedProvider([*(reply("", ("Greet", {"name": n})) for n in names),
                                 reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"greeter": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("greeter", "greet everyone",
                              config={"prompts": {"tool/Greet": "Say hi."}})
    await rt.run_until_idle()
    outputs = [r.event.result.text for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert outputs == ["Hi, Ada!", "Hi, Bob!", "Hi, Cy!"]
    first, *_, last = provider.requests
    [greet] = [t for t in first.tools if t.name == "Greet"]
    assert greet.description == "Say hi."  # the user's override of tool/Greet
    reminders = [m.text for m in last.messages if "greeted enough" in m.text]
    assert len(reminders) == 1
    assert "greeted enough" not in " ".join(m.text for m in provider.requests[2].messages)
    [notice] = [r.event.data["text"] for r in await client.events(tid)
                if isinstance(r.event, ExtensionRecorded) and r.event.kind == "notice"]
    assert notice == "hello: 3 greetings, asked the agent to stop"
    assert all(notice not in m.text for m in last.messages)  # the model never sees it
    await store.close()


async def test_greet_without_a_name_asks_the_person(tmp_path: Path) -> None:
    [agent] = build_agents([parse_agent_file(AGENT)], model="m")
    provider = ScriptedProvider([reply("", ("Greet", {})), reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"greeter": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("greeter", "greet someone")
    await rt.run_until_idle()
    [asked] = pending(await client.events(tid), tid)
    assert asked.detail["questions"][0]["question"] == "Who should I greet?"
    await answer_question(client, tid, asked.request_id, "Grace")
    await rt.run_until_idle()
    [greeted] = [r.event.result.text for r in await client.events(tid)
                 if isinstance(r.event, ToolFinished)]
    assert greeted == "Hi, Grace!"
    await store.close()


async def test_config_toml_adds_it_to_main(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path)
    (tmp_path / "config.toml").write_text(
        'model = "m"\napi_key = "k"\n'
        f'store = "sqlite:///{tmp_path / "t.db"}"\n'
        '[plugins.hello]\ngreeting = "Yo"\n'
        '[agents.main]\nadd_plugins = ["hello"]\n')
    app = from_settings(load_settings(), cwd=str(tmp_path))
    main = app.runtime.agents["main"]
    [hello] = [e for e in main.extensions if e.name == "hello"]
    assert hello.greeting == "Yo"  # type: ignore[attr-defined]
    ctx = CommandContext(cwd=str(tmp_path))
    assert await expand_command(main, ctx, "/greet Ada") == "Please greet Ada."
    await app.store.close()
