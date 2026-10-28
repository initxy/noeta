"""The date note: told once when the day moves past the one the environment
block gave, at the start of a turn; overridable text."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.plugins.environment import Environment
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


async def test_told_once_when_the_day_changes(tmp_path: Path) -> None:
    day = {"today": "2026-10-27"}
    env = Environment(today=lambda: day["today"])
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("a"), reply("b"), reply("c"), reply("d")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[env])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(tmp_path)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "hi", config={"prompts": {
        "reminder/date_changed.append": "(It is {date}.)"}})
    await rt.run_until_idle()
    assert "The date today is 2026-10-27." in [m.text for m in provider.requests[0].messages]

    def notes(i: int) -> list[str]:
        return [m.text for m in provider.requests[i].messages
                if m.role == "system" and m.text.startswith("The date moved on")]

    await client.send(tid, "same day")
    await rt.run_until_idle()
    assert notes(1) == []

    day["today"] = "2026-10-28"
    await client.send(tid, "next day")
    await rt.run_until_idle()
    assert notes(2) == ["The date moved on during this conversation: today is 2026-10-28. "
                        "The user knows, so do not point it out.\n\n(It is 2026-10-28.)"]

    await client.send(tid, "still the next day")
    await rt.run_until_idle()
    assert len(notes(3)) == 1
    await store.close()
