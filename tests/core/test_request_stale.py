"""``RequestStale``: a ``before_model`` that changed the conversation (a
compaction) has the request built again from the new state, every hook
seeing it, with one model call for the step."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import RequestStale
from noeta.core.events import Compacted, ExtensionRecorded, ModelCalled, ModelReplied
from noeta.core.extension import Extension, TaskView
from noeta.core.runner import Runtime
from noeta.core.types import ModelRequest
from noeta.testing import ScriptedProvider, reply


class CompactMidStep(Extension):
    """Compacts once the conversation has two prompts, then says the
    request it was handed is stale."""

    name = "compact"

    def __init__(self) -> None:
        self.raised = 0

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        prompts = [m for m in view.state.messages if m.role == "user" and m.origin == "human"]
        if len(prompts) >= 2 and view.state.compaction is None:
            view.record(self.name, "compacting", {"messages": len(request.messages)})
            view.compacted(Compacted(summary="(summary)", through=view.state.last_seq - 1))
            self.raised += 1
            raise RequestStale()
        return request


class Witness(Extension):
    """Runs after the compactor: the requests it is handed."""

    name = "witness"

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        self.seen.append([m.text for m in request.messages])
        return request


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_a_stale_request_is_built_again_from_the_compacted_state(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([reply("first"), reply("second")])
    compactor, witness = CompactMidStep(), Witness()
    agent = Agent(name="a", model="m", extensions=[compactor, witness])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("a", "one")
    await rt.run_until_idle()
    await client.send(tid, "two")
    await rt.run_until_idle()

    assert compactor.raised == 1
    # One model call per step: the stale request never went out.
    assert len(provider.requests) == 2
    sent = [m.text for m in provider.requests[1].messages]
    assert sent[0] == "(summary)" and sent[-1] == "two" and "one" not in sent
    # The hook after the compactor saw only the rebuilt request of that step.
    assert witness.seen[-1] == sent
    assert len(witness.seen) == 2
    events = [r.event for r in await client.events(tid)]
    kinds = [type(e).__name__ for e in events
             if isinstance(e, (Compacted, ModelCalled, ModelReplied))]
    assert kinds == ["ModelCalled", "ModelReplied", "Compacted", "ModelCalled", "ModelReplied"]
    # What the hook recorded before it raised stands.
    assert any(isinstance(e, ExtensionRecorded) and e.kind == "compacting" for e in events)
