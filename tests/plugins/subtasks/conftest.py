from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.store.sqlite import SqliteStore
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text
from noeta.testing import ScriptedProvider


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, providers: dict[str, ScriptedProvider], *agents: Agent,
            queues: tuple[str, ...] = ("default",)) -> Runtime:
    """Each agent's model name picks its scripted provider."""
    return Runtime(store=store, agents={a.name: a for a in agents},
                   provider_for=lambda m: providers[m], env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                   sleep=_no_sleep, queues=queues)


def echo(prefix: str = "did: ") -> Callable[[ModelRequest], ModelResponse]:
    """A child reply that echoes the child's prompt (its first message)."""

    def step(request: ModelRequest) -> ModelResponse:
        first = request.messages[0].text
        return ModelResponse(message=Message(role="assistant", content=[Text(text=prefix + first)]),
                             stop_reason="end_turn")

    return step


async def events(client: Client, tid: str) -> list[Any]:
    return [r.event for r in await client.events(tid)]


class Probe:
    name = "Probe"
    description = "Look."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        self.runs += 1
        return text_output("seen")
