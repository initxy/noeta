"""The task title: one side request at the first prompt of 10 characters or
more, structured output, recorded once; a greeting asks nothing."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest, ModelResponse
from noeta.plugins.title import LANGUAGE, PROMPT, SCHEMA, Title
from noeta.testing import ScriptedProvider, reply


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


class Provider(ScriptedProvider):
    """Main calls take the script; the title request gets a JSON title."""

    def __init__(self, *steps: ModelResponse) -> None:
        super().__init__(list(steps))
        self.titles: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, on_delta: object = None) -> ModelResponse:
        if request.output_schema == SCHEMA:
            self.titles.append(request)
            return reply(json.dumps({"title": "Parser crash in lexer.py"}))
        return await super().complete(request, on_delta)  # type: ignore[arg-type]


async def test_the_first_real_prompt_names_the_task_once(store: SqliteStore) -> None:
    provider = Provider(reply("hello"), reply("looking"), reply("again"))
    agent = Agent(name="a", model="claude-opus-5-5", identity="You are x.",
                  extensions=[Title()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("a", "hi")
    await rt.run_until_idle()
    assert provider.titles == []  # a greeting does not name the task
    await client.send(tid, "why does lexer.py crash on empty input?")
    await rt.run_until_idle()
    await client.send(tid, "and on tabs?")
    await rt.run_until_idle()
    [asked] = provider.titles
    # Claude Code 2.1.312 (m2/0001): identity line then the prompt, no tools,
    # no thinking field, the model's ceiling and the session's effort.
    assert [p.text for p in asked.system] == ["You are x.", PROMPT] and not asked.tools
    assert (asked.thinking, asked.effort, asked.max_tokens) == (None, "medium", 128_000)
    assert asked.messages[0].text == ("<session>\nwhy does lexer.py crash on empty input?\n"
                                      f"</session>\n\n{LANGUAGE}")
    titles = [e.data["title"] for e in (r.event for r in await client.events(tid))
              if isinstance(e, ExtensionRecorded) and e.extension == "title"
              and e.kind == "title"]
    assert titles == ["Parser crash in lexer.py"]
