"""Claude Code's task tools: which models get them, the tools' results, the
list as a fold of the log (another runtime, a fork), and the reminder after
ten model turns without them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import snapshots
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, Text, Thinking, ToolResult, ToolUse
from noeta.plugins.todos import (
    PROMPT_LINE,
    REMINDER,
    Todos,
    canonical,
    gets_task_tools,
    turns_since,
)
from noeta.plugins.todos.state import Item, TodoState
from noeta.plugins.todos.tools import (
    AGENT_STEER,
    LIST_STEER,
    TaskCreateTool,
    TaskUpdateTool,
    repair_create,
)
from noeta.testing import ScriptedProvider, reply

OLD = "claude-sonnet-4-5"


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


def _runtime(store: SqliteStore, provider: ScriptedProvider, model: str = OLD,
             todos: Todos | None = None) -> Runtime:
    agent = Agent(name="a", model=model, tools=[Probe()], extensions=[todos or Todos()])
    return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                   sleep=_no_sleep)


def _results(request: ModelRequest) -> list[str]:
    return [b.content[0].text for m in request.messages for b in m.content  # type: ignore[union-attr]
            if isinstance(b, ToolResult)]


def _view(model: str, parent: str | None = None) -> TaskView:
    return TaskView(state=TaskState(task_id="t", config={"model": model}, parent_id=parent),
                    model=model)


@pytest.mark.parametrize(("model", "gets"), [
    ("claude-sonnet-4-5", True), ("claude-sonnet-4-5-20250929", True), ("claude-opus-4-6", True),
    ("claude-opus-4-7", True), ("claude-haiku-4-5", True), ("claude-3-7-sonnet-latest", True),
    ("claude-opus-4-1-20250805", True), ("claude-opus-4-20250514", True),
    ("claude-sonnet-4-20250514", True), ("claude-opus-4", True), ("claude-sonnet-4", True),
    ("claude-opus-4-0", True), ("anthropic.claude-opus-4-20250514-v1:0", True),
    ("us.anthropic.claude-sonnet-4-6", True), ("eu.anthropic.claude-haiku-4-5-20251001-v1:0", True),
    ("arn:aws:bedrock:us-east-1:1:application-inference-profile/x", True),
    ("opensource/deepseek_v4_1_flash", False), ("gpt-5.5", False), ("deepseek-chat", False),
    ("claude-opus-5-5", False), ("opus", False), ("sonnet", False), ("claude-fable-5-1", False),
    ("claude-haiku-5-5", False), ("claude-opus-4-8", False), ("claude-sonnet-5", False)])
def test_the_models_claude_code_gives_the_tools(model: str, gets: bool) -> None:
    assert gets_task_tools(model) is gets


@pytest.mark.parametrize(("model", "name"), [
    ("claude-opus-4-20250514", "claude-opus-4-0"), ("claude-sonnet-4", "claude-sonnet-4-0"),
    ("anthropic.claude-opus-4-20250514-v1:0", "claude-opus-4-0"),
    ("claude-opus-4-1-20250805", "claude-opus-4-1"), ("claude-3-5-haiku-20241022",
                                                      "claude-3-5-haiku"),
    ("deepseek-v4-20260101", "deepseek-v4"),
    # Claude Code's order, quirks included.
    ("claude-opus-4-10", "claude-opus-4-1")])
def test_claude_codes_canonical_names(model: str, name: str) -> None:
    assert canonical(model) == name


def test_switches_and_sub_agents() -> None:
    names = [t.name for t in Todos().tools(_view(OLD))]
    assert names == ["TaskCreate", "TaskGet", "TaskUpdate", "TaskList"]
    assert all(getattr(t, "deferred", False) for t in Todos().tools(_view(OLD)))
    assert Todos().tools(_view("claude-opus-5-5")) == ()
    assert len(Todos(all_models=True).tools(_view("claude-opus-5-5"))) == 4
    # A sub-agent runs in the background: no task tools, as Claude Code's
    # background agents have none.
    assert Todos(all_models=True).tools(_view(OLD, parent="p")) == ()
    [part] = Todos().context(_view(OLD))
    assert part.text == PROMPT_LINE
    assert Todos().context(_view("claude-opus-5-5")) == ()


async def test_the_tools_results_and_the_list_folded_on_another_runtime(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("TaskCreate", {"subject": "A", "description": "do a"}),
              ("TaskCreate", {"title": "B", "content": "do b"})),
        reply("", ("TaskUpdate", {"taskId": "2", "addBlockedBy": ["1"]}),
              ("TaskUpdate", {"id": "1", "status": "in_progress"})),
        reply("", ("TaskList", {}), ("TaskGet", {"taskId": "2"}), ("TaskGet", {"taskId": "9"})),
        reply("", ("TaskUpdate", {"taskId": "1", "status": "completed",
                                  "metadata": {"k": 1}}), ("TaskList", {})),
        reply("", ("TaskUpdate", {"taskId": "2", "status": "deleted"}),
              ("TaskCreate", {"subject": "C", "description": "c"}),
              ("TaskUpdate", {"taskId": "2", "subject": "x"})),
        reply("done"),
    ])
    client = Client(store)
    tid = await client.create("a", "go")
    await _runtime(store, provider).run_until_idle()
    assert _results(provider.requests[-1]) == [
        "Task #1 created successfully: A", "Task #2 created successfully: B",
        "Updated task #2 blockedBy", "Updated task #1 status",
        "#1 [in_progress] A\n#2 [pending] B [blocked by #1]",
        "Task #2: B\nStatus: pending\nDescription: do b\nBlocked by: #1", "Task not found",
        "Updated task #1 metadata, status", "#1 [completed] A\n#2 [pending] B",
        # A deleted id is never given out again.
        "Updated task #2 deleted", "Task #3 created successfully: C", "Task not found"]

    # Another worker folds the same list from the log.
    again = ScriptedProvider([reply("", ("TaskList", {})), reply("ok")])
    await client.send(tid, "more")
    await _runtime(store, again).run_until_idle()
    assert _results(again.requests[-1])[-1] == "#1 [completed] A\n#3 [pending] C"

    # A task that continues this conversation starts with the list.
    forked = ScriptedProvider([reply("", ("TaskList", {})), reply("ok")])
    fork = await client.create("a", "fork", history_from=tid)
    await _runtime(store, forked).run_until_idle()
    assert _results(forked.requests[-1])[-1] == "#1 [completed] A\n#3 [pending] C"
    row = await client.get(fork)
    assert row is not None
    await store.close()


async def test_claude_5_models_get_neither_tools_nor_reminder(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([*(reply("", ("Probe", {})) for _ in range(12)), reply("done")])
    await Client(store).create("a", "go")
    await _runtime(store, provider, model="claude-opus-5-5").run_until_idle()
    await store.close()
    assert not any(t.name.startswith("Task") for r in provider.requests for t in r.tools)
    assert not any(REMINDER.split("\n")[0] in m.text for r in provider.requests
                   for m in r.messages)
    assert not any(PROMPT_LINE in p.text for r in provider.requests for p in r.system)


async def test_reminded_after_ten_turns_without_the_tools(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("TaskCreate", {"subject": "A", "description": "a"})),
        *(reply("", ("Probe", {})) for _ in range(21)), reply("done")])
    await Client(store).create("a", "go")
    await _runtime(store, provider).run_until_idle()
    await store.close()

    def reminders(i: int) -> list[str]:
        return [m.text for m in provider.requests[i].messages
                if m.role == "system" and m.text.startswith("The task tools")]

    # Request k follows reply k: ten turns after the TaskCreate (reply 1)
    # the result of reply 11 carries the reminder, with the list; ten turns
    # later, again.
    counts = [len(reminders(i)) for i in range(len(provider.requests))]
    assert counts == [0] * 11 + [1] * 10 + [2] * 2
    assert reminders(11)[0] == REMINDER + "\nHere are the existing tasks:\n#1. [pending] A"
    assert any(PROMPT_LINE in p.text for p in provider.requests[0].system)


def test_turns_count_content_blocks_and_skip_thinking() -> None:
    def step(*blocks: Any) -> list[Message]:
        uses = [b for b in blocks if isinstance(b, ToolUse)]
        return [Message(role="assistant", content=list(blocks)),
                Message(role="user", content=[ToolResult(call_id=u.id, content=[Text(text="ok")])
                                              for u in uses])]

    messages = [*step(ToolUse(id="1", name="TaskUpdate", input={})),
                *step(Thinking(text="hm"), Text(text="a"), ToolUse(id="2", name="Probe",
                                                                    input={})),
                *step(Text(text="b"))]
    # Never reminded: every turn shown counts.
    assert turns_since(messages, messages, -1) == (3, 4)
    # Reminded after the third turn of the history.
    assert turns_since(messages, messages, 3) == (3, 1)
    # A compaction left two turns: no count reaches back past it.
    assert turns_since(messages[2:4], messages, -1) == (2, 2)
    assert turns_since(messages[2:4], [*messages, *messages], 0) == (2, 2)


async def test_input_repairs_and_hints() -> None:
    records: list[tuple[str, str, Any]] = []
    ctx = ToolContext(task_id="t", call_id="c", env=None,  # type: ignore[arg-type]
                      state={}, record=lambda e, k, d: records.append((e, k, d)))
    out = await TaskCreateTool().run({"tasks": [{"subject": "a"}]}, ctx)
    assert out.is_error and out.content[0].text.startswith(  # type: ignore[union-attr]
        f"<tool_use_error>{LIST_STEER} InputValidationError: TaskCreate failed")
    out = await TaskCreateTool().run({"prompt": "p", "subagent_type": "x"}, ctx)
    assert AGENT_STEER in out.content[0].text  # type: ignore[union-attr]
    assert records == []
    out = await TaskCreateTool().run({"task": "Write the tests for the parser"}, ctx)
    assert out.content[0].text == (  # type: ignore[union-attr]
        "Task #1 created successfully: Write the tests for the parser")
    assert repair_create({"description": "x" * 50 + " " + "y" * 50}) == {
        "description": "x" * 50 + " " + "y" * 50, "subject": "x" * 50}
    assert repair_create({"subject": "s", "description": "d"}) is None
    out = await TaskUpdateTool().run({"taskId": "1", "status": "done"}, ctx)
    assert out.is_error and "`status`" in out.content[0].text  # type: ignore[union-attr]


def test_the_folded_list_snapshots() -> None:
    state = TaskState(task_id="t", ext={"todos": TodoState(items=(
        Item(id="1", subject="a", description="b", blocks=("2",), metadata={"k": [1]}),
        Item(id="2", subject="c", description="d", blocked_by=("1",))), highest=3)})
    data = snapshots.encode(state)
    assert data is not None and snapshots.decode(data) == state
