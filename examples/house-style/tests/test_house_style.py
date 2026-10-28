"""The house_style example: the style is captured when a task starts, so a
resumed task sends identical system prompt bytes after the file changed; the
language reminder rides once per person's turn."""

from __future__ import annotations

from pathlib import Path

from noeta import Agent, Runtime, function_tool
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.extension import ToolSet
from noeta.testing import ScriptedProvider, reply

HERE = Path(__file__).parents[1]


@function_tool(read_only=True)
def Look() -> str:
    """Look around."""
    return "nothing here"


async def test_the_style_is_pinned_at_task_start(tmp_path: Path) -> None:
    style = tmp_path / "house.md"
    style.write_text("---\nname: house\n---\nShort sentences. No emoji.\n")
    registry.load_plugin_files([HERE])
    plugin = registry.load(registry.PLUGINS, "house_style")(file=str(style), language="French")
    assert plugin.add_to == ("*",)
    agent = Agent(name="main", model="m", system_prompt="You help.",
                  extensions=(plugin, ToolSet([Look])))
    provider = ScriptedProvider([reply("", ("Look", {})), reply("bonjour"), reply("encore"),
                                 reply("neuf")])
    store = SqliteStore(str(tmp_path / "t.db"))

    def runtime() -> Runtime:  # a fresh worker each time, as after a restart
        return Runtime(store=store, agents={"main": agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: None)  # type: ignore[arg-type,return-value]

    client = Client(store)
    tid = await client.create("main", "hi")
    await runtime().run_until_idle()
    style.write_text("Long sentences.\n")
    await client.send(tid, "again")
    await runtime().run_until_idle()
    systems = {repr(r.system) for r in provider.requests}
    assert len(systems) == 1 and "Short sentences. No emoji." in str(systems.pop())
    language = [sum("Reply in French." in m.text for m in r.messages)
                for r in provider.requests]
    assert language == [1, 1, 2]  # once per person's turn, kept in the conversation
    other = await client.create("main", "new task")
    await runtime().run_until_idle()
    assert "Long sentences." in str(provider.requests[-1].system)
    assert other != tid
    await store.close()
