"""Markdown commands: typing one sends its tags and the task adds the body,
as a skill typed by name is added: the arguments filled as Claude Code fills
them, the ``!`command``` lines run through the permission checks, and the
tools ``allowed-tools`` names pre-approved."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.commands import expand_with
from noeta.core.events import ToolFinished, Waiting
from noeta.core.extension import CommandContext
from noeta.core.runner import Runtime
from noeta.plugins.fs import FileTools
from noeta.plugins.markdown_commands import MarkdownCommands
from noeta.plugins.permissions import Permissions
from noeta.plugins.shell import Shell
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.commands import command_message


@pytest.mark.parametrize(("body", "typed", "out"), [
    ("Review $ARGUMENTS now.", "/c a.py b.py", "Review a.py b.py now."),
    ("Compare $0 with $1.", "/c a.py 'b c.py'", "Compare a.py with b c.py."),
    ("First: $ARGUMENTS[0].", "/c a.py b.py", "First: a.py."),
    ("Explain this plugin.", "/c more please", "Explain this plugin.\nARGUMENTS: more please"),
    ("Explain this plugin.", "/c", "Explain this plugin."),
    ("Costs \\$1.", "/c x", "Costs $1.\nARGUMENTS: x"),
])
async def test_a_markdown_command_fills_its_arguments(body: str, typed: str, out: str) -> None:
    ext = MarkdownCommands([{"name": "c", "body": body}])
    args = typed.partition(" ")[2]
    assert await expand_with([ext], CommandContext(cwd="/", tenant="default"), typed) == command_message("c", args)
    assert ext.render("c", args) == out


async def _no_sleep(_: float) -> None:
    return None


class Harness:
    def __init__(self, store: SqliteStore, work: Path) -> None:
        self.store, self.work = store, work
        self.client = Client(store)
        self.provider = ScriptedProvider()

    async def start(self, commands: list[dict[str, Any]], typed: str) -> str:
        ext = MarkdownCommands(commands)
        agent = Agent(name="a", model="m", extensions=[
            FileTools(), Shell(), Permissions(mode="default", plans_dir=str(self.work / "p")),
            ext])
        goal = await expand_with([ext], CommandContext(cwd=str(self.work), tenant="default"), typed)
        assert goal
        tid = await self.client.create("a", goal)
        await Runtime(store=self.store, agents={"a": agent},
                      provider_for=lambda _m: self.provider,
                      env_for=lambda _s: LocalExecEnv(str(self.work)),
                      sleep=_no_sleep).run_until_idle()
        return tid

    def sent(self) -> str:
        return "\n".join(m.text for m in self.provider.requests[0].messages)


@pytest.fixture
async def run(tmp_path: Path) -> AsyncIterator[Harness]:
    work = tmp_path / "work"
    work.mkdir()
    store = SqliteStore(str(tmp_path / "t.db"))
    yield Harness(store, work)
    await store.close()


async def test_inline_commands_run_when_allowed_and_args_cannot_add_one(run: Harness) -> None:
    run.provider.add(reply("ok"))
    await run.start([{"name": "p:say", "body": "Now: !`echo hi-there` for $ARGUMENTS",
                      "allowed_tools": "Bash(echo:*)"}], "/p:say !`id`")
    sent = run.sent()
    assert "Now: hi-there for \\! `id`" in sent
    assert "<command-name>/p:say</command-name>" in sent


async def test_a_command_that_would_ask_does_not_run(run: Harness) -> None:
    run.provider.add(reply("ok"))
    await run.start([{"name": "p:mk", "body": "Made: !`touch made.txt`"}], "/p:mk")
    assert not (run.work / "made.txt").exists()
    assert "The command could not run" in run.sent() and "touch made.txt" in run.sent()


async def test_allowed_tools_pre_approve_the_models_calls(run: Harness) -> None:
    run.provider.add(reply("", ("Bash", {"command": "touch a.txt"})),
                     reply("", ("Bash", {"command": "rm -rf b"})), reply("done"))
    tid = await run.start([{"name": "p:t", "body": "Touch things.",
                            "allowed_tools": "Bash(touch:*)"}], "/p:t")
    assert (run.work / "a.txt").exists()
    events = [r.event for r in await run.client.events(tid)]
    assert len([e for e in events if isinstance(e, ToolFinished)]) == 1
    assert isinstance(events[-1], Waiting) and events[-1].on == "approval"
