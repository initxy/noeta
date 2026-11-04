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
from noeta.plugins.plan_mode import plan as pl
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime, with_plans


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


LOAD = ("ToolSearch", {"query": "select:ExitPlanMode", "max_results": 1})


def system_texts(request: Any) -> list[str]:
    return [m.text for m in request.messages if m.role == "system"]


class Namer(ScriptedProvider):
    """Main calls take the script; plan-name requests are kept and named."""

    def __init__(self, steps: list[Any]) -> None:
        super().__init__(steps)
        self.named: list[Any] = []

    async def complete(self, request: Any, on_delta: object = None) -> Any:
        if request.output_schema == pl.PLAN_NAME_SCHEMA:
            self.named.append(request)
            return reply('{"name": "add-logging"}')
        return await super().complete(request, on_delta)  # type: ignore[arg-type]


async def test_planning_end_to_end(store: SqliteStore, tmp_path: Path) -> None:
    write = Fake("Write")
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    ag = agent(with_plans(perms), [write])
    provider = Namer([reply("planned")])
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

    provider.add(reply("", ("Write", {"file_path": path}),
                       ("Write", {"file_path": str(tmp_path / "proj" / "a.py")})))
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
    provider.add(reply("", LOAD), reply("", ("ExitPlanMode", {})), reply("coding now"))
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
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    finished = [e for e in await events(client, tid) if isinstance(e, ToolFinished)][-1]
    assert finished.result.is_error and "not on" in finished.result.text
    # It says how to plan, and that an approved plan means carrying on.
    assert "EnterPlanMode" in finished.result.text and "approved" in finished.result.text


def test_plan_name_parsing() -> None:
    assert pl.parse_plan_name('{"name": "Add Logging!"}') == "add-logging"
    assert pl.parse_plan_name("nope") is None
    request = pl.plan_name_request("m", "x" * 2000)
    assert len(request.messages[0].text) < 1100
    assert request.output_schema == pl.PLAN_NAME_SCHEMA


async def test_the_plan_is_named_once_approved_not_when_shown(store: SqliteStore,
                                                              tmp_path: Path) -> None:
    # Claude Code 2.1.312 (m1/0061 -> 0062): the name is asked for after the
    # person approves, from the plan's first 1,000 characters; a plan sent
    # back is not named.
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    ag = agent(with_plans(perms), [])
    provider = Namer([reply("planning")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    message = await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan logging")
    assert message is not None
    tid = await client.create("a", message, config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    [path] = [e.data["plan"]["path"] for e in await events(client, tid)
              if isinstance(e, ExtensionRecorded) and e.kind == "mode"
              and e.data["mode"] == "plan"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("# Plan\n\n1. log\n")
    provider.add(reply("", LOAD), reply("", ("ExitPlanMode", {})))
    await client.send(tid, "ready")
    await rt.run_until_idle()
    question = next(p for p in pending(await client.events(tid), tid) if p.kind == "question")
    assert provider.named == []
    await client.answer(tid, question.request_id,
                        data={"answers": {"0": {"other": "shorter please"}}})
    provider.add(reply("", ("ExitPlanMode", {})))
    await rt.run_until_idle()
    assert provider.named == []
    question = next(p for p in pending(await client.events(tid), tid) if p.kind == "question")
    provider.add(reply("coding now"))
    await client.answer(tid, question.request_id,
                        data={"answers": {"0": {"selected": [pl.CHOICE_DEFAULT]}}})
    await rt.run_until_idle()
    [named] = provider.named
    assert named.messages[0].text.startswith("<conversation>\n# Plan")
    assert [p.text for p in named.system] == [pl.PLAN_NAME_PROMPT]
    assert named.thinking is False  # an uncatalogued model: reasoning off
    names = [e.data["name"] for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "plan_name"]
    assert names == ["add-logging"]



async def test_only_the_first_approved_plan_of_a_task_is_named(store: SqliteStore,
                                                               tmp_path: Path) -> None:
    # Claude Code 2.1.312 names a plan only while the session has no name
    # (``if(!D&&Bg(K()))return``, and again when the name arrives): a second
    # approved plan in the same task asks for no name.
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    ag = agent(with_plans(perms), [])
    provider = Namer([reply("planning")])
    rt = runtime(store, provider, ag)
    client = Client(store)
    message = await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan logging")
    assert message is not None
    tid = await client.create("a", message, config={"cwd": str(tmp_path)})
    await rt.run_until_idle()

    async def approve(note: str) -> None:
        [path] = [e.data["plan"]["path"] for e in await events(client, tid)
                  if isinstance(e, ExtensionRecorded) and e.kind == "mode"
                  and e.data["mode"] == "plan"][-1:]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(f"# Plan\n\n1. {note}\n")
        provider.add(reply("", LOAD), reply("", ("ExitPlanMode", {})))
        await client.send(tid, "ready")
        await rt.run_until_idle()
        question = next(p for p in pending(await client.events(tid), tid)
                        if p.kind == "question")
        provider.add(reply("coding now"))
        await client.answer(tid, question.request_id,
                            data={"answers": {"0": {"selected": [pl.CHOICE_DEFAULT]}}})
        await rt.run_until_idle()

    await approve("log")
    assert len(provider.named) == 1
    provider.add(reply("planning again"))
    again = await expand_command(ag, CommandContext(cwd=str(tmp_path)), "/plan metrics")
    assert again is not None
    await client.send(tid, again)
    await rt.run_until_idle()
    await approve("metrics")
    assert len(provider.named) == 1
    names = [e.data["name"] for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "plan_name"]
    assert names == ["add-logging"]

ENTER = ("ToolSearch", {"query": "select:EnterPlanMode", "max_results": 1})


async def test_enter_plan_mode_asks_the_person_then_plans(store: SqliteStore,
                                                          tmp_path: Path) -> None:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("", ENTER), reply("", ("EnterPlanMode", {})),
                                 reply("planning")])
    rt = runtime(store, provider, agent(with_plans(perms), []))
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
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", "add caching", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    evs = await events(client, tid)
    assert not pending(await client.events(tid), tid)
    first, second = [e.result.text for e in evs if isinstance(e, ToolFinished)][-2:]
    assert first == pl.ENTERED and second == pl.ALREADY_PLANNING


FULL, SPARSE = "Plan mode is on.", "Plan mode is still on"


def plan_texts(request: Any) -> list[str]:
    """The plan-mode texts this request adds after its newest prompt."""
    last = max(i for i, m in enumerate(request.messages) if m.from_person)
    found: list[tuple[int, int, str]] = []
    for i, m in enumerate(request.messages[last:]):
        if m.role != "system":
            continue
        for marker in (FULL, SPARSE, "## Back in plan mode"):
            at = m.text.find(marker)
            if at >= 0:
                found.append((i, at, m.text[at:]))
    return [text for _i, _at, text in sorted(found)]


async def test_a_task_created_in_plan_mode_plans_from_its_first_request(
        store: SqliteStore, tmp_path: Path) -> None:
    """``noeta -p --permission-mode plan``: the task's config starts it in
    plan mode, with a plan file, the plan-mode text on its first request and
    edits held for the person."""
    write = Fake("Write")
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("", ("Write", {"file_path": str(tmp_path / "a.py")})),
                                 reply("ok")])
    rt = runtime(store, provider, agent(with_plans(perms), [write]))
    client = Client(store)
    tid = await client.create("a", "add caching",
                              config={"cwd": str(tmp_path), "permission_mode": "plan"})
    await rt.run_until_idle()
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    assert entered["mode"] == "plan" and entered["plan"]["pre_mode"] == "default"
    [text] = plan_texts(provider.requests[0])
    assert text.startswith(FULL) and entered["plan"]["path"] in text
    assert write.runs == []


async def test_a_switch_that_comes_with_the_prompt_is_told_at_once(
        store: SqliteStore, tmp_path: Path) -> None:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("ok")])
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", config={"cwd": str(tmp_path)})
    await client.signal(tid, "permission_mode", {"mode": "plan"})
    await client.send(tid, "add caching")
    await rt.run_until_idle()
    [text] = plan_texts(provider.requests[0])
    assert text.startswith(FULL)


async def test_the_plan_text_follows_claude_codes_cadence(
        store: SqliteStore, tmp_path: Path) -> None:
    """Full on entering; nothing for the next four prompts; then the short
    form; full again on every fifth text."""
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("ok") for _ in range(26)])
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", "p1", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    for i in range(2, 27):
        await client.send(tid, f"p{i}")
        await rt.run_until_idle()
    kinds = [("full" if t[0].startswith(FULL) else "sparse") if (t := plan_texts(r)) else None
             for r in provider.requests]
    sent = {i + 1: k for i, k in enumerate(kinds) if k}
    assert sent == {1: "full", 6: "sparse", 11: "sparse", 16: "sparse", 21: "sparse",
                    26: "full"}


async def test_a_prompt_that_arrives_mid_turn_counts_toward_the_cadence_at_once(
        store: SqliteStore, tmp_path: Path) -> None:
    """Checked before every model call (Claude Code's plan_mode attachment):
    the fifth prompt since the full text, sent while a call runs, brings the
    short text with the very next call."""
    client = Client(store)

    class Inject:
        name = "Inject"
        description = "send a message mid-turn"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = True
        clearable = False

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            from noeta.core.tool import text_output
            await client.send(ctx.task_id, "p6", when="now")
            return text_output("sent")

    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"),
                        risk={"Inject": "low"})
    provider = ScriptedProvider([*[reply("ok") for _ in range(4)],
                                 reply("", ("Inject", {})), reply("ok")])
    rt = runtime(store, provider, agent(with_plans(perms), [Inject()]))
    tid = await client.create("a", "p1", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    for i in range(2, 6):
        await client.send(tid, f"p{i}")
        await rt.run_until_idle()
    texts = [plan_texts(r) for r in provider.requests]
    assert [bool(t) for t in texts] == [True, False, False, False, False, True]
    assert not texts[-1][0].startswith(FULL)


async def test_planning_again_with_a_plan_file_reads_it_first(
        store: SqliteStore, tmp_path: Path) -> None:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("ok") for _ in range(3)])
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", "plan it", config={"cwd": str(tmp_path),
                                                      "permission_mode": "plan"})
    await rt.run_until_idle()
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    path = Path(entered["plan"]["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Plan\n")
    for mode, prompt in (("default", "build it"), ("plan", "plan the next part")):
        await client.signal(tid, "permission_mode", {"mode": mode})
        await client.send(tid, prompt)
        await rt.run_until_idle()
    again = plan_texts(provider.requests[-1])
    assert again[0].startswith("## Back in plan mode") and str(path) in again[0]
    assert again[1].startswith(FULL)


async def test_a_sub_agent_of_a_planning_task_plans_too(
        store: SqliteStore, tmp_path: Path) -> None:
    """Claude Code's sub-agents run in their parent's mode, plan mode too,
    with the sub-agent form of the text; they are offered no ExitPlanMode."""

    class Spawn:
        name = "Spawn"
        description = "start a child"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = True
        clearable = False

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            from noeta.core.tool import text_output
            assert ctx.spawn is not None
            ctx.spawn("a", "look around", background=True)
            return text_output("started")

    write = Fake("Write")
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"),
                        risk={"Spawn": "low"})
    provider = ScriptedProvider([reply("", ("Spawn", {})),
                                 reply("", ("Write", {"file_path": str(tmp_path / "a.py")})),
                                 reply("child done"), reply("parent done")])
    rt = runtime(store, provider, agent(with_plans(perms), [Spawn(), write]))
    client = Client(store)
    tid = await client.create("a", "plan it", config={"cwd": str(tmp_path),
                                                      "permission_mode": "plan"})
    await rt.run_until_idle()
    child = next(r for r in provider.requests if any(m.text == "look around"
                                                       for m in r.messages))
    [text] = plan_texts(child)
    assert "Answer the query you were given fully" in text
    # Its plan file is named after the parent's (Claude Code's
    # <plan name>-agent-<agent id>.md).
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    [child_id] = [t.task_id for t in await client.tasks(parent_id=tid)]
    stem = Path(entered["plan"]["path"]).stem
    assert str(tmp_path / "plans" / f"{stem}-agent-{child_id}.md") in text
    assert not any(t.name in ("ExitPlanMode", "EnterPlanMode") for t in child.tools)
    assert write.runs == []


def test_a_task_keeps_the_first_plan_name() -> None:
    from noeta.plugins.plan_mode.state import PlanState, fold

    st = fold(PlanState(), "plan_exit", {"mode": "default", "named": True})
    assert st.plan_named and st.plan_name is None
    st = fold(fold(st, "plan_name", {"name": "add-logging"}), "plan_name", {"name": "other"})
    assert st.plan_name == "add-logging"
    # A log written before ``named``: the recorded name still counts.
    assert fold(PlanState(), "plan_name", {"name": "x"}).plan_named


async def _exit_plan(store: SqliteStore, tmp_path: Path, plan: str | None,
                     answer: dict[str, Any]) -> tuple[Any, Any]:
    perms = Permissions(mode="default", plans_dir=str(tmp_path / "plans"))
    provider = ScriptedProvider([reply("planning"), reply("", LOAD),
                                 reply("", ("ExitPlanMode", {})), reply("ok")])
    rt = runtime(store, provider, agent(with_plans(perms), []))
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path),
                                                 "permission_mode": "plan"})
    await rt.run_until_idle()
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    if plan is not None:
        path = Path(entered["plan"]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan)
    await client.send(tid, "ready")
    await rt.run_until_idle()
    [question] = [p for p in pending(await client.events(tid), tid) if p.kind == "question"]
    await client.answer(tid, question.request_id, data={"answers": {"0": answer}})
    await rt.run_until_idle()
    result = [e for e in await events(client, tid) if isinstance(e, ToolFinished)][-1].result
    return question, result


async def test_exit_plan_mode_without_a_plan_file_still_asks(store: SqliteStore,
                                                             tmp_path: Path) -> None:
    """Claude Code asks with no plan written, and its approval says so."""
    question, result = await _exit_plan(store, tmp_path, None,
                                        {"selected": [pl.CHOICE_DEFAULT]})
    assert question.detail["questions"][0]["question"] == pl.EXIT_NO_PLAN_QUESTION
    assert not result.is_error and result.text == pl.APPROVED_NO_PLAN


async def test_a_rejected_plan_comes_back_with_the_rejection(store: SqliteStore,
                                                             tmp_path: Path) -> None:
    _, result = await _exit_plan(store, tmp_path, "# Plan\n\n1. log\n",
                                 {"selected": [pl.CHOICE_KEEP]})
    assert result.is_error
    assert result.text == pl.KEEP_PLANNING_PLAN.format(plan="# Plan\n\n1. log\n")


@pytest.mark.parametrize(("service", "pre", "classify"), [
    ("auto", "default", False),   # the person left auto mode before planning
    ("default", "auto", True),    # planning began in auto mode
])
async def test_a_sub_agent_plans_as_its_parent_does(
        store: SqliteStore, tmp_path: Path, service: str, pre: str, classify: bool) -> None:
    """A planning task's sub-agent uses the auto-mode classifier when its
    parent's plan does, whatever the service's default mode."""
    from noeta.core.loop.state import TaskState
    from noeta.plugins.permissions.state import PermState
    from noeta.plugins.plan_mode.state import AUTO_DURING_PLAN, PlanState

    perms = Permissions(mode=service, plans_dir=str(tmp_path / "plans"))
    plan = {"path": str(tmp_path / "plans" / "p.md"), "pre_mode": pre, "entered_seq": 3,
            AUTO_DURING_PLAN: classify}
    parent = TaskState(ext={"permissions": PermState(mode="plan", plan=plan),
                            "plan_mode": PlanState(mode="plan", plan=plan)})
    exts = with_plans(perms)
    config = {"cwd": str(tmp_path), **{k: v for e in exts for k, v in e.inherit(parent).items()}}
    assert config["permission_mode"] == "plan"
    provider = ScriptedProvider([reply("done")])
    rt = runtime(store, provider, agent(with_plans(perms)))
    client = Client(store)
    tid = await client.create("a", "look around", config=config)
    await rt.run_until_idle()
    [entered] = [e.data for e in await events(client, tid)
                 if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    assert entered["plan"]["pre_mode"] == pre
    assert entered["plan"][AUTO_DURING_PLAN] is classify
