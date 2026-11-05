"""The eval's per-trial usage counts the replies the kernel dropped and the
empty-reply nudges, from a real task log."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evals.engine import subtask_events, trial_usage
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import Message, ModelResponse, Thinking, Usage
from noeta.plugins.empty_reply import EmptyReply
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


async def test_trial_usage_counts_dropped_replies_and_nudges(tmp_path: Path) -> None:
    cut = ModelResponse(message=Message(role="assistant", content=[Thinking(text="hmm")]),
                        stop_reason="max_tokens", usage=Usage(uncached=10, output=7))
    provider = ScriptedProvider([cut, reply(""), reply("hi"), reply(""), reply("")])
    store = SqliteStore(str(tmp_path / "t.db"))
    agent = Agent(name="a", model="m", extensions=[EmptyReply()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    await client.send(tid, "again")
    await rt.run_until_idle()
    usage = trial_usage([r.event for r in await client.events(tid)])
    await store.close()
    # cut + empty + "hi", then empty + empty: five calls, two nudges.
    assert usage["requests"] == 5
    assert usage["empty_nudges"] == 2
    assert usage["output_tokens"] == 7 + 4 * 5
    assert usage["input_tokens"] == 50


async def test_subtask_events_walks_every_level_below_the_root() -> None:
    children = {"root": ["a", "b"], "a": ["a1"], "b": [], "a1": []}

    class FakeClient:
        async def tasks(self, *, parent_id: str, limit: int) -> list[Any]:
            return [SimpleNamespace(task_id=c) for c in children[parent_id]]

        async def events(self, task_id: str) -> list[Any]:
            return [SimpleNamespace(event=f"{task_id}-1"), SimpleNamespace(event=f"{task_id}-2")]

    got = await subtask_events(FakeClient(), "root")  # type: ignore[arg-type]
    assert sorted(got) == ["a-1", "a-2", "a1-1", "a1-2", "b-1", "b-2"]
