"""Plan mode and permissions each work alone: plan mode without the
permissions plugin keeps planning read-only by itself, permissions without
plan mode offers no plan mode at all, and a log written while both were one
plugin replays the same."""

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
from noeta.plugins.permissions.state import PermState
from noeta.plugins.plan_mode import PlanMode
from noeta.plugins.plan_mode import plan as pl
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.permission_modes import current_mode, published_modes
from tests.plugins.permissions.helpers import Fake, agent, events, runtime

LOAD = ("ToolSearch", {"query": "select:ExitPlanMode", "max_results": 1})


class Reader(Fake):
    read_only = True


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def _mode_records(evs: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in evs if isinstance(e, ExtensionRecorded)
            and e.extension == "permissions" and e.kind in ("mode", "plan_exit")]


async def test_plan_mode_alone_plans_read_only_and_exits(store: SqliteStore,
                                                         tmp_path: Path) -> None:
    """No permissions plugin: a read-only tool and the plan file's write
    run, any other change is asked about; ExitPlanMode asks the person and
    the approved plan ends planning, after which nothing is asked."""
    read, write = Reader("Read"), Fake("Write")
    ag = agent([PlanMode(plans_dir=str(tmp_path / "plans"))], [read, write])
    provider = ScriptedProvider([reply("planning")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    message = await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan add logging")
    assert message is not None
    tid = await client.create("a", message, config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [entered] = _mode_records(await events(client, tid))
    path = entered["plan"]["path"]
    assert entered["plan"]["pre_mode"] == "default"

    provider.add(reply("", ("Read", {"file_path": str(tmp_path / "a.py")}),
                       ("Write", {"file_path": path, "content": "# Plan\n"}),
                       ("Write", {"file_path": str(tmp_path / "a.py")})))
    await client.send(tid, "go on")
    await rt.run_until_idle()
    texts = [m.text for m in provider.requests[1].messages if m.role == "system"]
    assert any("Plan mode is on" in t for t in texts)
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval" and len(last.ids) == 1
    assert [r["file_path"] for r in write.runs] == [path] and len(read.runs) == 1
    [ask] = [p for p in pending(await client.events(tid), tid) if p.kind == "approval"]
    assert "plan mode" in str(ask.detail.get("reason"))
    await client.answer(tid, ask.request_id, decision="deny")
    await rt.run_until_idle()

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("# Plan\n\n1. log\n")
    provider.add(reply("", LOAD), reply("", ("ExitPlanMode", {})), reply("approved"))
    await client.send(tid, "ready")
    await rt.run_until_idle()
    [question] = [p for p in pending(await client.events(tid), tid) if p.kind == "question"]
    await client.answer(tid, question.request_id,
                        data={"answers": {"0": {"selected": [pl.CHOICE_DEFAULT]}}})
    await rt.run_until_idle()
    result = [e for e in await events(client, tid) if isinstance(e, ToolFinished)][-1].result
    assert not result.is_error and "approved the plan" in result.text
    assert current_mode(await client.events(tid)) == "default"

    provider.add(reply("", ("Write", {"file_path": str(tmp_path / "a.py")})), reply("done"))
    await client.send(tid, "build it")
    await rt.run_until_idle()
    assert [r["file_path"] for r in write.runs] == [path, str(tmp_path / "a.py")]


async def test_plan_mode_alone_switches_by_signal(store: SqliteStore, tmp_path: Path) -> None:
    """A front end's switch into plan mode and out of it, with no
    permissions plugin to record the other modes."""
    ag = agent([PlanMode(plans_dir=str(tmp_path / "plans"))], [])
    provider = ScriptedProvider([reply("ok"), reply("ok")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "hi", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    await client.signal(tid, "permission_mode", {"mode": "plan"})
    await client.send(tid, "plan")
    await rt.run_until_idle()
    assert current_mode(await client.events(tid)) == "plan"
    await client.signal(tid, "permission_mode", {"mode": "acceptEdits"})
    await client.send(tid, "go")
    await rt.run_until_idle()
    assert current_mode(await client.events(tid)) == "acceptEdits"
    texts = [m.text for m in provider.requests[-1].messages if m.role == "system"]
    assert any("Plan mode has ended" in t for t in texts)


async def test_a_task_created_in_plan_mode_without_permissions(store: SqliteStore,
                                                               tmp_path: Path) -> None:
    write = Fake("Write")
    ag = agent([PlanMode(plans_dir=str(tmp_path / "plans"))], [write])
    provider = ScriptedProvider([reply("", ("Write", {"file_path": str(tmp_path / "a.py")}))])
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    assert any("Plan mode is on" in m.text for m in provider.requests[0].messages)
    assert not write.runs
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval"


async def test_permissions_alone_offer_no_plan_mode(store: SqliteStore,
                                                    tmp_path: Path) -> None:
    """No plan-mode plugin: no plan tools or ``/plan``, ``plan`` is not
    offered to front ends, a switch to it or a config naming it is ignored,
    and nothing plan-related reaches the model."""
    write = Fake("Write")
    ag = agent([Permissions(mode="acceptEdits", plans_dir=str(tmp_path / "plans"))], [write])
    assert await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan x") is None
    provider = ScriptedProvider([reply("", ("Write", {"file_path": str(tmp_path / "a.py")})),
                                 reply("ok"), reply("ok")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    assert len(write.runs) == 1
    await client.signal(tid, "permission_mode", {"mode": "plan"})
    await client.send(tid, "again")
    await rt.run_until_idle()
    recorded = await client.events(tid)
    assert current_mode(recorded) == "acceptEdits"
    offered = published_modes(recorded)
    assert offered is not None and "plan" not in offered.modes and "plan" not in offered.cycle
    for request in provider.requests:
        assert not any(t.name in (pl.ENTER_PLAN_MODE, pl.EXIT_PLAN_MODE) for t in request.tools)
        assert not any("plan mode" in m.text.lower() for m in request.messages)


async def test_with_neither_plugin_a_plan_config_changes_nothing(store: SqliteStore,
                                                                 tmp_path: Path) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Write", {"file_path": str(tmp_path / "a.py")})),
                                 reply("done")])
    rt = runtime(store, provider, agent([], [write]))
    client = Client(store)
    await client.create("a", "go", config={"cwd": str(tmp_path), "permission_mode": "plan"})
    await rt.run_until_idle()
    assert len(write.runs) == 1


def test_coordination_tools_may_run_while_planning() -> None:
    """The tools the permissions plugin rates low risk that are not
    read-only say so themselves, so plan mode alone lets them run."""
    from noeta.core.tool import tool_flags
    from noeta.plugins.ask_user import AskUserQuestionTool
    from noeta.plugins.schedule import WakeupTool
    from noeta.plugins.shell.tools import TaskStopTool
    from noeta.plugins.skills.tool import SkillTool
    from noeta.plugins.subtasks.tool import AgentTool, HandbackTool, SendMessageTool
    from noeta.plugins.todos.tools import TaskCreateTool, TaskUpdateTool
    from noeta.plugins.workflow.tool import WorkflowTool

    for cls in (AskUserQuestionTool, WakeupTool, TaskStopTool, SkillTool, AgentTool,
                HandbackTool, SendMessageTool, TaskCreateTool, TaskUpdateTool, WorkflowTool):
        assert tool_flags(cls).safe_while_planning, cls  # type: ignore[arg-type]


async def test_plan_mode_alone_lets_the_person_be_asked_and_tasks_be_listed(
        store: SqliteStore, tmp_path: Path) -> None:
    from noeta.plugins.ask_user import AskUser
    from noeta.plugins.todos import Todos

    question = {"questions": [{"question": "Which approach?", "header": "Approach",
                               "multiSelect": False,
                               "options": [{"label": "A", "description": "a"},
                                           {"label": "B", "description": "b"}]}]}
    ag = agent([PlanMode(plans_dir=str(tmp_path / "plans")), AskUser(),
                Todos(all_models=True)], [])
    provider = ScriptedProvider([
        reply("", ("TaskCreate", {"subject": "explore", "description": "look around"})),
        reply("", ("AskUserQuestion", question))])
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    [created] = [e for e in await events(client, tid) if isinstance(e, ToolFinished)]
    assert not created.result.is_error
    kinds = [p.kind for p in pending(await client.events(tid), tid)]
    assert kinds == ["question"]


async def test_a_planning_sub_agent_starts_and_reports_without_permissions(
        store: SqliteStore, tmp_path: Path) -> None:
    """A sub-agent of a planning task plans too; with plan mode alone the
    ``Agent`` call starts it and its report comes back without anyone being
    asked (outside auto mode its last reply is the report)."""
    from noeta.core.agent import Agent
    from noeta.core.events import TaskEnded
    from noeta.plugins.subtasks import SubagentSpec, Subtasks
    from tests.plugins.subtasks.conftest import runtime as subtasks_runtime

    plans = str(tmp_path / "plans")
    roster = [SubagentSpec(name="general-purpose", description="Looks.", agent="worker")]
    main = Agent(name="main", model="p", extensions=[Subtasks(roster), PlanMode(plans_dir=plans)])
    worker = Agent(name="worker", model="c",
                   extensions=[Subtasks(()), PlanMode(plans_dir=plans)])
    p = ScriptedProvider([reply("", ("Agent", {"description": "look", "prompt": "look",
                                               "subagent_type": "general-purpose"})),
                          reply("launched"), reply("thanks")])
    c = ScriptedProvider([reply("found it")])
    rt = subtasks_runtime(store, {"p": p, "c": c}, main, worker)
    client = Client(store)
    tid = await client.create("main", "plan", config={"cwd": str(tmp_path),
                                                      "permission_mode": "plan"})
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    end = [r.event for r in await client.events(child.task_id)][-1]
    assert isinstance(end, TaskEnded) and end.answer == "found it"
    assert any("Plan mode is on" in m.text for m in c.requests[0].messages)
    assert not pending(await client.events(tid), tid)


def test_a_log_left_planning_reads_as_its_starting_mode_without_plan_mode() -> None:
    """A log from before the split that was planning, read by a host
    without the plan-mode plugin: the mode the plan began in."""
    from types import SimpleNamespace

    from noeta.core.loop.state import TaskState

    perms = Permissions(mode="default")
    st = PermState(mode="plan", plan={"path": "/p.md", "pre_mode": "acceptEdits"})

    def view(*exts: Any) -> Any:
        return SimpleNamespace(ext_state=lambda name: st, pending=[], extensions=exts,
                               state=TaskState(config={}))

    assert perms.mode_of(view(perms)) == "acceptEdits"
    assert perms.mode_of(view(perms, PlanMode())) == "plan"
