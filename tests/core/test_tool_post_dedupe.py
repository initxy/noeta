"""A tool's posts to its task's inbox keep their ``dedupe_key``, also when a
workflow posts through its tool context: a retried post is one item."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import External
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.workflow import _ToolView
from noeta.testing import ScriptedProvider, reply


class Poster:
    name = "Poster"
    description = "posts twice"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.post is not None
        for _ in range(2):
            await ctx.post(External(name="ping"), dedupe_key="ping:1")
        return text_output("posted")


async def _no_sleep(_: float) -> None:
    return None


async def test_a_tool_post_is_idempotent_on_its_dedupe_key(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    try:
        provider = ScriptedProvider([reply("", ("Poster", {})), reply("done")])
        rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", tools=[Poster()])},
                     provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                     sleep=_no_sleep)
        tid = await Client(store).create("a", "go")
        await rt.run_until_idle()
        pings = [i for i in await store.inbox(tid)
                 if isinstance(i.body, External) and i.body.name == "ping"]
        assert len(pings) == 1
    finally:
        await store.close()


async def test_the_workflow_tool_view_passes_the_dedupe_key_on() -> None:
    posted: list[tuple[Any, str | None]] = []

    async def post(body: Any, task_id: str | None = None,
                   dedupe_key: str | None = None) -> None:
        posted.append((body, dedupe_key))

    view = _ToolView(types.SimpleNamespace(post=post))  # type: ignore[arg-type]
    assert view.post is not None
    await view.post("body", dedupe_key="workflow:end:r1")
    assert posted == [("body", "workflow:end:r1")]
