"""Memory: the tools (write merge rules), the index, recall, the judge,
the index-change line, the curator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import ModelRequest
from noeta.plugins.memory import (
    CONSOLIDATION_AGENT_NAME,
    MEMORY_POLICY_PROMPT,
    Memory,
    MemoryStore,
    consolidation_agent,
)
from noeta.plugins.memory import store as store_mod
from noeta.plugins.memory.index import RECALLED_BODY_FRAME, render_memory_index_text
from noeta.plugins.memory.tools import (
    MemoryArchiveTool,
    MemoryReadTool,
    MemorySearchTool,
    MemoryWriteTool,
)
from noeta.testing import reply
from tests.plugins.context.conftest import Harness


def ctx(root: Path, task_id: str = "t1") -> ToolContext:
    return ToolContext(task_id=task_id, call_id="c", env=None,  # type: ignore[arg-type]
                       state={"memory": {"root": str(root)}}, record=lambda *a: None)


def out(o: ToolOutput) -> Any:
    assert not o.is_error, o.content
    return json.loads(o.content[0].text)  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_mod, "_today", lambda: "2026-10-27")


# ---------------------------------------------------------------- tools


async def test_write_merges_fields_and_stamps(tmp_path: Path) -> None:
    w = MemoryWriteTool("memory")
    c = ctx(tmp_path)
    assert out(await w.run({"name": "deploy", "text": "Deploy from main.\n",
                            "keywords": "ship, 部署", "type": "procedural"}, c)) == \
        {"name": "deploy", "bytes": len((tmp_path / "deploy.md").read_bytes())}
    # A body-only rewrite keeps every field it does not mention.
    await w.run({"name": "deploy", "text": "Deploy from release.\n"}, c)
    text = (tmp_path / "deploy.md").read_text()
    assert text == ("---\ntype: procedural\nkeywords: ship, 部署\ncreated: 2026-10-27\n"
                    "updated: 2026-10-27\nsource_task: t1\n---\nDeploy from release.\n")
    # Empty removes; a fence-only text keeps the body.
    o = out(await w.run({"name": "deploy", "text": "---\nkeywords:\n---\n"}, c))
    assert "existing body kept" in o["note"]
    text = (tmp_path / "deploy.md").read_text()
    assert "keywords" not in text and text.endswith("Deploy from release.\n")


async def test_write_refusals_and_similar_hint(tmp_path: Path) -> None:
    w = MemoryWriteTool("memory", max_bytes=200)
    c = ctx(tmp_path)
    bad = await w.run({"name": "../x", "text": "t"}, c)
    assert bad.is_error and bad.content[0].text == "memory_write: invalid memory name '../x'"  # type: ignore[union-attr]
    fence = await w.run({"name": "new", "text": "---\ntype: user\n---\n"}, c)
    assert fence.is_error and "a new memory needs a body" in fence.content[0].text  # type: ignore[union-attr]
    big = await w.run({"name": "big", "text": "x" * 300}, c)
    assert big.is_error and "over this store's cap of 200" in big.content[0].text  # type: ignore[union-attr]
    await w.run({"name": "deploy-process", "text": "How we deploy the app.\n"}, c)
    o = out(await w.run({"name": "deploy-steps", "text": "How we deploy the app, stepwise.\n"},
                        c))
    assert o["similar"] == ["deploy-process"]


async def test_read_search_archive(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    await MemoryWriteTool("memory").run({"name": "a", "text": "alpha line\nbeta\n"}, c)
    got = out(await MemoryReadTool("memory").run({"name": "a"}, c))
    assert got["text"].endswith("alpha line\nbeta\n") and got["truncated"] is False
    found = out(await MemorySearchTool("memory").run({"query": "ALPHA"}, c))
    assert found == {"query": "ALPHA", "results": [{"name": "a", "lines": ["alpha line"]}],
                     "total": 1, "truncated": False}
    assert out(await MemoryArchiveTool("memory").run({"name": "a"}, c)) == \
        {"name": "a", "archived_to": "archive/a.md"}
    missing = await MemoryReadTool("memory").run({"name": "a"}, c)
    assert missing.is_error and missing.content[0].text == "memory_read: no memory named 'a'"  # type: ignore[union-attr]


def test_descriptions_are_0_6_bytes() -> None:
    w = MemoryWriteTool("memory")
    assert w.description.startswith("Persist a named memory as a markdown file;")
    assert not w.description.endswith("\n")
    assert MEMORY_POLICY_PROMPT.startswith("Memory: you have cross-session memory tools")
    assert MEMORY_POLICY_PROMPT.endswith("rather than leaving it stale.\n")


def test_the_policy_continues_the_agents_prompt() -> None:
    from noeta.core.extension import TaskView
    from noeta.core.loop.request import build_request
    from noeta.core.loop.state import TaskState

    view = TaskView(state=TaskState())
    request = build_request(TaskState(), model="m", system_prompt="P\n",
                            parts=Memory(root="/tmp/x").context(view), tools=[])
    assert [p.text for p in request.system] == ["P\n\n" + MEMORY_POLICY_PROMPT]


# ---------------------------------------------------------------- in a task


def write_page(root: Path, name: str, body: str, **fields: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fence = "".join(f"{k}: {v}\n" for k, v in fields.items())
    (root / f"{name}.md").write_text(f"---\n{fence}---\n{body}" if fields else body)


def agent(root: Path, **kw: Any) -> Agent:
    return Agent(name="a", model="m", extensions=[Memory(root=root, **kw)])


async def test_index_once_and_recall_after_the_message(h: Harness, tmp_path: Path) -> None:
    root = tmp_path / "mem"
    write_page(root, "deploy-process", "Deploy from main with make ship.\n",
               description="How we deploy")
    write_page(root, "coffee", "Likes espresso.\n", keywords="drink")
    a = agent(root)
    h.provider.add(reply("ok"), reply("again"), reply("third"))
    tid = await h.start(a, "how does the deploy process work?")
    msgs = h.provider.requests[0].messages
    assert msgs[0].text.startswith("Long-term memory index.") and msgs[0].origin == "system"
    assert "- deploy-process: How we deploy" in msgs[0].text
    assert msgs[1].text == "how does the deploy process work?"
    assert msgs[2].origin == "memory"
    assert msgs[2].text == (f"{RECALLED_BODY_FRAME}\n\n## deploy-process\n---\n"
                            "description: How we deploy\n---\nDeploy from main with make ship.\n")
    # Already in context: silent. A keyword match: a pointer.
    await h.say(a, tid, "deploy process again; also a drink?")
    tail = h.last_messages()[-1]
    assert tail.origin == "memory" and tail.text.startswith("Possibly relevant memories")
    assert "- coffee: Likes espresso." in tail.text and "deploy-process" not in tail.text
    assert len(await h.records(tid, "memory", "index")) == 1


async def test_a_memory_read_page_is_silent_and_exclude_is_honoured(
        h: Harness, tmp_path: Path) -> None:
    root = tmp_path / "mem"
    write_page(root, "deploy-process", "Deploy notes.\n")
    write_page(root, "release-train", "Release notes.\n")
    a = agent(root, exclude=["release-train"])
    h.provider.add(reply("", ("memory_read", {"name": "deploy-process"})), reply("ok"),
                   reply("fine"))
    tid = await h.start(a, "hello")
    await h.say(a, tid, "the deploy process and the release train?")
    assert await h.records(tid, "memory", "recalled") == []
    assert await h.records(tid, "memory", "pointers") == []


async def test_related_pages_ride_as_pointers(h: Harness, tmp_path: Path) -> None:
    root = tmp_path / "mem"
    write_page(root, "deploy-process", "Deploy.\n", related="rollback-plan")
    write_page(root, "rollback-plan", "Roll back.\n", description="If it breaks")
    h.provider.add(reply("ok"))
    tid = await h.start(agent(root), "explain the deploy process")
    [pointers] = await h.records(tid, "memory", "pointers")
    assert pointers.data == {"names": ["rollback-plan"]}
    assert "- rollback-plan: If it breaks" in h.provider.requests[0].messages[-1].text


async def test_judge_picks_when_nothing_matched(h: Harness, tmp_path: Path) -> None:
    root = tmp_path / "mem"
    write_page(root, "deploy-process", "Deploy.\n", description="Shipping to prod",
               keywords="ship")
    judged: list[ModelRequest] = []

    def judge(request: ModelRequest) -> Any:
        judged.append(request)
        return reply('Sure: ["deploy-process", "made-up"]')

    h.provider.add(judge, reply("ok"))
    tid = await h.start(agent(root, recall_model="small"), "how do we push to production")
    assert judged[0].model == "small" and judged[0].max_tokens == 200
    assert "Memory index:\n- deploy-process: Shipping to prod [aliases: ship]" in \
        judged[0].system[0].text
    [pointers] = await h.records(tid, "memory", "pointers")
    assert pointers.data == {"names": ["deploy-process"]}


async def test_index_change_line_after_a_write(h: Harness, tmp_path: Path) -> None:
    root = tmp_path / "mem"
    write_page(root, "old", "Old.\n")
    a = agent(root)
    h.provider.add(reply("", ("memory_write", {"name": "fresh", "text": "Brand new fact.\n"})),
                   reply("saved"), reply("ok"), reply("ok2"))
    tid = await h.start(a, "remember something")
    await h.say(a, tid, "and now?")
    assert h.last_messages()[-1].text == \
        "Memory index changed this task: +fresh (Brand new fact.)"
    await h.say(a, tid, "still?")
    assert len(await h.records(tid, "memory", "delta")) == 1


async def test_root_callable_is_resolved_once(h: Harness, tmp_path: Path) -> None:
    calls: list[str] = []

    async def root(view: Any) -> Path:
        calls.append(view.task_id)
        return tmp_path / "tenant"

    a = Agent(name="a", model="m", extensions=[Memory(root=root)])
    h.provider.add(reply("", ("memory_write", {"name": "n", "text": "body\n"})), reply("ok"),
                   reply("ok"))
    tid = await h.start(a)
    await h.say(a, tid, "again")
    assert calls == [tid]
    assert (tmp_path / "tenant" / "n.md").exists()


async def test_read_only_offers_two_tools(h: Harness, tmp_path: Path) -> None:
    h.provider.add(reply("ok"))
    await h.start(agent(tmp_path / "mem", read_only=True))
    assert [t.name for t in h.provider.requests[0].tools] == ["memory_read", "memory_search"]


def test_index_budget_degrades_whole_lines() -> None:
    entries = tuple((f"page-{i:02d}", "A summary " * 5, "", "") for i in range(30))
    updated = {f"page-{i:02d}": f"2026-10-{i + 1:02d}" for i in range(30)}
    text = render_memory_index_text(entries, budget_tokens=150, updated=updated)
    assert "- page-29" in text and "older memories are not listed here" in text
    full = render_memory_index_text(entries[:2], budget_tokens=10_000)
    assert "- page-00: A summary" in full


def test_consolidation_agent() -> None:
    curator = consolidation_agent("m", root="/tmp/x")
    assert curator.name == CONSOLIDATION_AGENT_NAME and curator.one_shot
    assert curator.system_prompt.startswith("You are the memory-consolidation agent")
    assert MEMORY_POLICY_PROMPT not in curator.system_prompt  # the extension adds it
    [mem] = curator.extensions
    assert isinstance(mem, Memory) and not mem.recall


def test_store_ignores_invalid_names_and_archive(tmp_path: Path) -> None:
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "gone.md").write_text("old\n")
    (tmp_path / ".hidden.md").write_text("x\n")
    (tmp_path / "页面.md").write_text("中文\n")
    assert [e[0] for e in MemoryStore(tmp_path).entries()] == ["页面"]
