"""What follows a summary, laid out as Claude Code lays it out: the
workspace context ahead of the summary, the date and the attribution note
around kept messages, the environment and the lists given again in the
request's last system turn; and the summarize call's beta flags."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.events import Compacted
from noeta.core.types import Message
from noeta.plugins.compaction import Compaction
from noeta.plugins.environment import Environment, date_line

from ._harness import Model, Probe, answer, call, run, runtime
from .test_compaction_runtime import SMALL, big_call

TODAY = "2026-10-28"
GIT_HEAD = "Here is context from the workspace that may help you answer:"
NOTE_HEAD = "From here on, end the git commit messages"


def _trailing_system(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in reversed(messages):
        if m.role != "system":
            break
        out.insert(0, m)
    return out


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_what_follows_a_summary(store: SqliteStore, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    a = Agent(name="a", model="claude-opus-5-5", tools=[Probe()],
              extensions=(Environment(today=lambda: TODAY),
                          Compaction(auto_compact_window=SMALL)))
    model = Model([call(), big_call(), big_call(), answer("done")])
    client, tid = await run(store, a, model, env_for=lambda _s: LocalExecEnv(str(repo)))
    kept, everything = [r.event for r in await client.events(tid)
                        if isinstance(r.event, Compacted)]

    # Keeping the newest reply: the git snapshot ahead of the summary, the
    # date right after it, the attribution note after the kept results.
    assert [m.text.startswith(GIT_HEAD) for m in kept.lead] == [True]
    assert [m.text for m in kept.kept] == [date_line(TODAY)]
    assert kept.after[-1].text.startswith(NOTE_HEAD)
    first = next(r for r in model.requests if len(r.messages) > 1
                 and r.messages[1].text == kept.summary)
    assert [m.role for m in first.messages[2:4]] == ["system", "assistant"]
    # The environment again, in the last system turn (joined on the wire),
    # with the budget note and no second date.
    last = "\n\n".join(m.text for m in _trailing_system(first.messages))
    assert "# Environment" in last and "<total_tokens>" in last
    assert date_line(TODAY) not in last

    # Summarizing everything: the snapshot and the note both lead it; the
    # date ends the system turn; no budget note follows a summary.
    assert [m.text.startswith((GIT_HEAD, NOTE_HEAD)) for m in everything.lead] == [True, True]
    assert everything.lead[1].text.startswith(NOTE_HEAD) and not everything.kept
    second = next(r for r in model.requests
                  if any(m.text == everything.summary for m in r.messages))
    assert [m.role for m in second.messages[:3]] == ["user", "user", "user"]
    joined = "\n\n".join(m.text for m in _trailing_system(second.messages))
    assert len(second.messages) == 3 + len(_trailing_system(second.messages))
    assert joined.endswith(date_line(TODAY)) and "<total_tokens>" not in joined

    # The summarize call adds Claude Code's summarize beta.
    assert all("fallback-credit-2026-06-01" in s.betas for s in model.summarize_requests)


class _Safe(Probe):
    """A tool auto mode runs without asking its classifier."""

    name = "ReportFindings"


async def test_the_auto_mode_note_waits_for_new_input_after_a_compaction(
        store: SqliteStore) -> None:
    from noeta.plugins.permissions import Permissions

    a = Agent(name="a", model="claude-opus-5-5", tools=[_Safe()],
              extensions=(Permissions(), Compaction(auto_compact_window=SMALL)))
    model = Model([call("ReportFindings"), big_call("ReportFindings"),
                   big_call("ReportFindings"), answer("done"), answer("ok")])
    client, tid = await run(store, a, model)
    kept, everything = [r.event for r in await client.events(tid)
                        if isinstance(r.event, Compacted)]

    def noted(i: int) -> bool:
        return any(m.role == "system" and "Auto mode is on" in m.text
                   for m in model.requests[i].messages)

    def after(c: Compacted) -> int:
        return next(i for i, r in enumerate(model.requests)
                    if any(m.text == c.summary for m in r.messages))

    # Not in the request right after a summary (whatever it kept): only once
    # a prompt or a tool result comes in after it.
    assert noted(0) and not noted(after(kept)) and not noted(after(everything))
    await client.send(tid, "more")
    await runtime(store, a, model).run_until_idle()
    assert noted(len(model.requests) - 1)


async def test_a_first_request_over_the_threshold_sends_the_environment_once(
        store: SqliteStore, tmp_path: Path) -> None:
    """A first prompt already over the threshold: nothing before it to
    summarize, so the request goes as it is, with one environment block
    (a compaction of the prompt alone used to add a second)."""
    a = Agent(name="a", model="m", tools=[Probe()],
              extensions=(Environment(today=lambda: TODAY),
                          Compaction(auto_compact_window=SMALL)))
    model = Model([answer("read it")])
    client, tid = await run(store, a, model, goal="x" * 40_000,
                            env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    assert not [r for r in await client.events(tid) if isinstance(r.event, Compacted)]
    [request] = model.requests
    texts = [m.text for m in request.messages]
    assert sum(t.startswith("# Environment") for t in texts) == 1
    assert sum(t == date_line(TODAY) for t in texts) == 1
