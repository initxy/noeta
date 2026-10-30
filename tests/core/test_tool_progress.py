"""A running tool's progress (``ToolContext.progress``) reaches whoever
watches the task live, and never the log or the model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec

from noeta.backends.stores.postgres_feed import delta_payloads
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Delta
from noeta.testing import ScriptedProvider, reply


class Slow:
    name = "Slow"
    description = "s"
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.progress is not None
        ctx.progress("half way")
        ctx.progress("almost")
        return text_output("done")


async def test_progress_is_streamed_with_its_call_and_not_recorded(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    seen: list[tuple[str, Delta]] = []
    provider = ScriptedProvider([reply("", ("Slow", {})), reply("ok")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", tools=[Slow()])},
                 provider_for=lambda _m: provider,
                 on_delta=lambda tid, d: seen.append((tid, d)))
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    progress = [d for t, d in seen if d.kind == "tool" and t == tid]
    assert [d.text for d in progress] == ["half way", "almost"]
    [call] = provider.requests[1].messages[-2].tool_uses
    assert {(d.call_id, d.tool) for d in progress} == {(call.id, "Slow")}
    assert all(d.elapsed >= 0 for d in progress)
    log = str([r.event for r in await client.events(tid)])
    assert "half way" not in log and "half way" not in str(provider.requests)
    await store.close()


def test_progress_crosses_processes_as_its_tail() -> None:
    [payload] = delta_payloads([("t", Delta(kind="tool", text="x" * 5000 + "end",
                                            call_id="c1", tool="Bash"))])
    [(task, delta)] = msgspec.json.decode(payload, type=list[tuple[str, Delta]])
    assert task == "t" and delta.text.endswith("end") and len(delta.text) < 5000
    assert (delta.call_id, delta.tool) == ("c1", "Bash")
