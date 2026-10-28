"""Plan mode: ``/plan``, what may run while planning, the plan file, the
plan-mode texts, ``ExitPlanMode``'s approval question and the plan name."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.commands import expand_command
from noeta.core.events import ExtensionRecorded, ToolFinished, Waiting
from noeta.core.extension import CommandContext
from noeta.host.pending import pending
from noeta.plugins.permissions import Permissions
from noeta.plugins.permissions import plan as pl
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


LOAD = ("ToolSearch", {"query": "select:ExitPlanMode", "max_results": 1})


def system_texts(request: Any) -> list[str]:
    return [m.text for m in request.messages if m.role == "system"]


async def test_planning_end_to_end(store: SqliteStore, tmp_path: Path) -> None:
    write = Fake("Write")
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    ag = agent([perms], [write])
    provider = ScriptedProvider([reply("planned")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    message = await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan add logging")
    assert message is not None and message.startswith("<command-name>/plan</command-name>")
    tid = await client.create("a", message, config={"cwd": str(tmp_path / "proj")})
    await rt.run_until_idle()
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"
                 and e.data["mode"] == "plan"]
    path = entered["plan"]["path"]
    assert path.startswith(str(tmp_path / "plans")) and path.endswith(".md")
    # The /plan turn itself carries no plan-mode text; the next prompt does.
    assert not any("Plan mode is on" in t for t in system_texts(provider.requests[0]))

    provider.add(reply("", ("Write", {"file_path": str(tmp_path / "proj" / "a.py")}),
                       ("Write", {"file_path": path})))
    await client.send(tid, "go on")
    await rt.run_until_idle()
    texts = system_texts(provider.requests[1])
    assert any("Plan mode is on" in t and path in t and "does not exist yet" in t
               for t in texts)
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval" and len(last.ids) == 1
    assert [r["file_path"] for r in write.runs] == [path]
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "deny")
    await rt.run_until_idle()

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("# Plan\n\n1. log\n")
    provider.add(reply("", LOAD), reply("", ("ExitPlanMode", {})),
                 reply('{"name": "add-logging"}'),
                 reply("coding now"))
    await client.send(tid, "ready")
    await rt.run_until_idle()
    [question] = [p for p in pending(await client.events(tid), tid) if p.kind == "question"]
    labels = [o["label"] for o in question.detail["questions"][0]["options"]]
    assert labels == [pl.CHOICE_ACCEPT_EDITS, pl.CHOICE_DEFAULT, pl.CHOICE_KEEP]
    assert "# Plan" in question.detail["questions"][0]["question"]
    await client.answer(tid, question.request_id,
                        data={"answers": {"0": {"selected": [pl.CHOICE_ACCEPT_EDITS]}}})
    await rt.run_until_idle()
    evs = await events(client, tid)
    result = [e for e in evs if isinstance(e, ToolFinished)][-1].result
    assert not result.is_error and "## Approved Plan:\n# Plan" in result.text
    modes = [e.data["mode"] for e in evs if isinstance(e, ExtensionRecorded)
             and e.kind in ("mode", "plan_exit")]
    assert modes[-1] == "acceptEdits"
    exited = system_texts(provider.requests[-1])
    assert any("Plan mode has ended" in t for t in exited)


async def test_exit_plan_mode_outside_plan_mode_and_keep_planning(store: SqliteStore,
                                                                  tmp_path: Path) -> None:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("", LOAD), reply("", ("ExitPlanMode", {})),
                                 reply("ok")])
    rt = runtime(store, provider, agent([perms], []))
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    finished = [e for e in await events(client, tid) if isinstance(e, ToolFinished)][-1]
    assert finished.result.is_error and "not on" in finished.result.text


def test_plan_name_parsing() -> None:
    assert pl.parse_plan_name('{"name": "Add Logging!"}') == "add-logging"
    assert pl.parse_plan_name("nope") is None
    request = pl.plan_name_request("m", "x" * 2000)
    assert len(request.messages[0].text) < 1100
    assert request.output_schema == pl.PLAN_NAME_SCHEMA


ENTER = ("ToolSearch", {"query": "select:EnterPlanMode", "max_results": 1})


async def test_enter_plan_mode_asks_the_person_then_plans(store: SqliteStore,
                                                          tmp_path: Path) -> None:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("", ENTER), reply("", ("EnterPlanMode", {})),
                                 reply("planning")])
    rt = runtime(store, provider, agent([perms], []))
    client = Client(store)
    tid = await client.create("a", "add caching", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [item] = pending(await client.events(tid), tid)
    assert "Enter plan mode?" in str(item.detail)
    await client.answer(tid, item.request_id, "allow")
    await rt.run_until_idle()
    evs = await events(client, tid)
    finished = [e for e in evs if isinstance(e, ToolFinished)][-1]
    assert not finished.result.is_error and finished.result.text == pl.ENTERED
    [entered] = [e.data for e in evs if isinstance(e, ExtensionRecorded)
                 and e.kind == "mode" and e.data["mode"] == "plan"]
    assert entered["plan"]["pre_mode"] == "default"
    # The next request carries the plan-mode instructions.
    assert any("Plan mode is on" in t for t in system_texts(provider.requests[-1]))


async def test_enter_plan_mode_in_auto_mode_needs_no_approval(store: SqliteStore,
                                                              tmp_path: Path) -> None:
    perms = Permissions(mode="auto", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("", ENTER), reply("", ("EnterPlanMode", {})),
                                 reply("", ("EnterPlanMode", {})), reply("planning")])
    rt = runtime(store, provider, agent([perms], []))
    client = Client(store)
    tid = await client.create("a", "add caching", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert not pending(await client.events(tid), tid)
    first, second = [e.result.text for e in evs if isinstance(e, ToolFinished)][-2:]
    assert first == pl.ENTERED and second == pl.ALREADY_PLANNING
