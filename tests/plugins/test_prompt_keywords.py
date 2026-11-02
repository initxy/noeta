"""Claude Code's prompt keywords: ``ultrathink`` (a note, and ``xhigh``
effort for the turn when a person follows the task) and ``ultracode`` (a
note while ``Workflow`` is offered), and the task's ``ultracode`` switch."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.loop.state import CONFIG_SIGNAL
from noeta.core.runner import Runtime
from noeta.plugins.environment import Environment
from noeta.plugins.workflow import Orchestration
from noeta.plugins.workflow.opt_in import (
    KEYWORD_NOTE,
    ULTRACODE_EVERY,
    ULTRACODE_OFF,
    ULTRACODE_ON,
    ULTRACODE_STILL,
)
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.ultrathink import ULTRATHINK_NOTE


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


class Probe:
    name = "Probe"
    description = "Look."
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = True

    async def run(self, input: dict[str, Any], ctx: Any) -> Any:
        from noeta.core.tool import text_output

        return text_output("seen")


def _texts(request: Any) -> list[str]:
    return [m.text for m in request.messages]


async def _session(store: SqliteStore, agent: Agent, steps: list[Any],
                   config: dict[str, Any] | None = None) -> tuple[Client, str, Any, Any]:
    provider = ScriptedProvider(steps)

    def runtime() -> Runtime:
        return Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]

    client = Client(store)
    tid = await client.create(agent.name, config=config)
    return client, tid, provider, runtime


async def test_ultrathink_adds_a_note_and_raises_the_turns_effort(store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", tools=[Probe()],
                  extensions=[Environment(today=lambda: "2026-11-01")])
    client, tid, provider, runtime = await _session(
        store, agent, [reply("", ("Probe", {})), reply("deep"), reply("plain")])
    await client.send(tid, "Please ULTRATHINK about the cache.")
    await runtime().run_until_idle()
    first, second = provider.requests
    assert ULTRATHINK_NOTE in _texts(first) and ULTRATHINK_NOTE not in _texts(second)[-2:]
    assert first.effort == "xhigh" and second.effort == "xhigh"  # the rest of the turn
    await client.send(tid, "now something quick")
    await runtime().run_until_idle()
    assert provider.requests[2].effort is None
    assert _texts(provider.requests[2]).count(ULTRATHINK_NOTE) == 1


async def test_ultrathink_keeps_the_effort_when_nobody_follows_the_task(
        store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", effort="high",
                  extensions=[Environment(today=lambda: "2026-11-01")])
    client, tid, provider, runtime = await _session(store, agent, [reply("ok")],
                                                    config={"interactive": False})
    await client.send(tid, "ultrathink: why?")
    await runtime().run_until_idle()
    [request] = provider.requests
    assert ULTRATHINK_NOTE in _texts(request) and request.effort == "high"


async def test_the_ultracode_keyword_opts_the_turn_in(store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", extensions=[Orchestration()])
    client, tid, provider, runtime = await _session(store, agent, [reply("a"), reply("b")])
    await client.send(tid, "ultracode: audit the repo")
    await runtime().run_until_idle()
    await client.send(tid, "thanks")
    await runtime().run_until_idle()
    first, second = provider.requests
    assert KEYWORD_NOTE in _texts(first)
    assert _texts(second).count(KEYWORD_NOTE) == 1


async def test_the_ultracode_switch_is_told_on_again_and_off(store: SqliteStore) -> None:
    agent = Agent(name="a", model="m", extensions=[Orchestration()])
    client, tid, provider, runtime = await _session(
        store, agent, [reply("ok") for _ in range(ULTRACODE_EVERY + 3)],
        config={"ultracode": True})

    def told(i: int) -> list[str]:
        """The switch notes after request ``i``'s newest prompt."""
        messages = provider.requests[i].messages
        last = max(j for j, m in enumerate(messages) if m.origin == "human")
        return [m.text for m in messages[last:]
                if m.text in (ULTRACODE_ON, ULTRACODE_STILL, ULTRACODE_OFF)]

    for i in range(ULTRACODE_EVERY + 1):
        await client.send(tid, f"p{i}")
        await runtime().run_until_idle()
    assert told(0) == [ULTRACODE_ON]
    assert all(told(i) == [] for i in range(1, ULTRACODE_EVERY))
    assert told(ULTRACODE_EVERY) == [ULTRACODE_STILL]
    await client.signal(tid, CONFIG_SIGNAL, {"ultracode": None})
    await client.send(tid, "off now")
    await runtime().run_until_idle()
    assert told(ULTRACODE_EVERY + 1) == [ULTRACODE_OFF]
