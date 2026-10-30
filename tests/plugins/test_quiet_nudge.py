"""The quiet-stretch nudge: after five tool-only steps in a row since the
user last spoke or the model last wrote text, as a system turn, and again
after every five more, three times at most per stretch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.quiet_nudge import NUDGE, QuietNudge
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


class Probe:
    name = "Probe"
    description = "probe"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output("ok")


async def test_nudged_every_five_quiet_steps_three_times_at_most(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    steps = [reply("Looking.", ("Probe", {})), *(reply("", ("Probe", {})) for _ in range(20)),
             reply("done")]
    provider = ScriptedProvider(steps)
    agent = Agent(name="a", model="m", tools=[Probe()], extensions=[QuietNudge()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 sleep=_no_sleep)
    await Client(store).create("a", "go")
    await rt.run_until_idle()
    await store.close()

    def nudges(i: int) -> int:
        return sum(m.role == "system" and m.text == NUDGE for m in provider.requests[i].messages)

    # Request 0 is the prompt; 1 follows the step with text; 2..21 follow
    # quiet steps 1..20: the results of quiet steps 5, 10 and 15 carry a
    # nudge, and the stretch gets no fourth.
    assert [nudges(i) for i in range(len(provider.requests))] == [
        sum(i >= k for k in (6, 11, 16)) for i in range(22)]


def test_a_question_the_person_answered_starts_the_stretch_again() -> None:
    from noeta.core.types import Message, Text, ToolResult, ToolUse
    from noeta.plugins.quiet_nudge import quiet_steps

    def step(name: str, i: int) -> list[Message]:
        return [Message(role="assistant", content=[ToolUse(id=f"c{i}", name=name, input={})]),
                Message(role="user", content=[ToolResult(call_id=f"c{i}",
                                                          content=[Text(text="ok")])])]

    messages = [*step("Probe", 0), *step("ExitPlanMode", 1),
                *(m for i in range(2, 6) for m in step("Probe", i))]
    assert quiet_steps(messages, NUDGE) == (4, 0)
