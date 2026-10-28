"""Shared bits for the permissions tests: fake tools and a runner."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.extension import Extension
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.testing import ScriptedProvider


class Fake:
    """A tool that records its runs and answers ``ran <name>``."""

    description = "fake"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs: list[dict[str, Any]] = []

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs.append(input)
        return text_output(f"ran {self.name}")


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, provider: ScriptedProvider, *agents: Agent) -> Runtime:
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep)


def agent(exts: Sequence[Extension], tools: Sequence[Any] = (), name: str = "a",
          one_shot: bool = False) -> Agent:
    return Agent(name=name, model="m", extensions=tuple(exts), tools=tuple(tools),
                 one_shot=one_shot)


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]
