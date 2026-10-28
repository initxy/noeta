"""The numbers behind /cost and /context: a task tree's usage by model, and
the estimated parts of the next request (``Runtime.preview``)."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import Usage
from noeta.host.usage import context_breakdown, context_text, cost_text, tree_usage
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


async def test_tree_usage_and_the_next_request(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("hi", usage=Usage(uncached=100, output=20,
                                                          cache_read=50))])
    agent = Agent(name="a", model="claude-opus-5-5", system_prompt="You help.")
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,  # type: ignore[arg-type,return-value]
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("a", "hello there", config={"model": "claude-opus-5-5"})
    await rt.run_until_idle()

    total = await tree_usage(client, tid)
    assert total.usage == Usage(uncached=100, output=20, cache_read=50)
    assert total.tasks == 1 and total.cost_usd > 0
    assert "claude-opus-5-5: 100 input, 20 output, 50 cache read" in cost_text(total)

    request = await rt.preview(tid)
    assert request is not None
    assert [m.text for m in request.messages] == ["hello there", "hi"]
    parts = context_breakdown(request)
    assert parts["categories"]["System prompt"] > 0 and parts["categories"]["Messages"] > 0
    assert parts["window"] > parts["total"]
    assert "Free space" in context_text(parts)
    assert await rt.preview("missing") is None
    await store.close()
