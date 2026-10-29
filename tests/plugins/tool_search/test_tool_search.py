"""Deferred tools (the ``tool_search`` plugin): announced by name, loaded
with ``ToolSearch``, then sent with ``defer_loading`` for the rest of the
task."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.providers.anthropic import AnthropicProvider
from noeta.backends.providers.openai_compat import OpenAICompatProvider
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import Compacted, ToolFinished
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.request import request_tools
from noeta.core.loop.state import TaskState
from noeta.core.runner import Runtime
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, Text, ToolReference, ToolResult, ToolSpec
from noeta.plugins.tool_search import PLACEHOLDER, ToolSearch, ToolSearchTool, loaded_tools
from noeta.testing import ScriptedProvider, reply


class Fake:
    read_only = True
    clearable = True
    input_schema: dict[str, Any] = {"type": "object"}

    def __init__(self, name: str, *, deferred: bool = False, last: bool = False,
                 description: str = "") -> None:
        self.name = name
        self.deferred = deferred
        self.listed_last = last
        self.description = description or f"The {name} tool."

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output(f"{self.name} ran")


def names(specs: Sequence[ToolSpec]) -> list[str]:
    return [s.name + ("*" if s.defer_loading else "") for s in specs]


def specs(tools: Sequence[Tool], loaded: Sequence[str] = ()) -> list[ToolSpec]:
    """The request's tool list with the plugin's changes, ``loaded`` named
    by an earlier tool result."""
    state = TaskState()
    if loaded:
        state._add(Message(role="user", content=[ToolResult(call_id="c", content=[
            ToolReference(tool_name=n) for n in loaded])]), 1)
    changes = ToolSearch().tool_changes(TaskView(state=state), tools)
    if changes is None:
        return request_tools(tools, {}, (), lambda t: t.description)
    return request_tools([*tools, *changes.add], changes.withhold, changes.entries,
                         lambda t: t.description)


def test_no_deferred_tool_means_a_sorted_list_and_no_placeholder() -> None:
    assert names(specs([Fake("Write"), Fake("Bash"), Fake("Agent")])) == [
        "Agent", "Bash", "Write"]


def test_the_placeholder_sits_before_the_last_tool_until_something_is_loaded() -> None:
    tools = [Fake("Write"), Fake("Read"), Fake("WebFetch", deferred=True),
             Fake("mcp__s__t", deferred=True, last=True), Fake("ToolSearch")]
    assert names(specs(tools)) == ["Read", "ToolSearch", PLACEHOLDER + "*", "Write"]
    assert names(specs(tools, ["WebFetch", "mcp__s__t"])) == [
        "Read", "ToolSearch", "WebFetch*", "Write", PLACEHOLDER + "*", "mcp__s__t*"]


def test_a_listed_last_tool_keeps_the_placeholder_before_it() -> None:
    tools = [Fake("Skill"), Fake("Bash"), Fake("ToolSearch"), Fake("Read"),
             Fake("Handback", last=True), Fake("WebFetch", deferred=True)]
    assert names(specs(tools)) == ["Bash", "Read", "Skill", "ToolSearch", PLACEHOLDER + "*",
                                   "Handback"]


def test_the_placeholder_schema_is_bare() -> None:
    [p] = [s for s in specs([Fake("X", deferred=True)]) if s.name == PLACEHOLDER]
    assert p.input_schema == {"type": "object", "properties": {}} and p.defer_loading


def test_search_forms() -> None:
    search = ToolSearchTool({t.name: t for t in [
        Fake("WebFetch", description="Fetch a web page and answer a prompt about it."),
        Fake("NotebookEdit", description="Edit a Jupyter notebook cell."),
        Fake("mcp__slack__send", description="Send a message to a channel."),
        Fake("mcp__mail__send", description="Send an email."),
    ]})
    assert search.search("select:NotebookEdit, webfetch,Nope") == ["NotebookEdit", "WebFetch"]
    assert search.search("jupyter notebook") == ["NotebookEdit"]
    assert search.search("+slack send") == ["mcp__slack__send"]
    assert search.search("send", max_results=1) == ["mcp__slack__send"]  # ties: tool order
    assert search.search("nothing here") == []


def test_keyword_ranking() -> None:
    search = ToolSearchTool({t.name: t for t in [
        Fake("WebFetch", description="Fetch a web page."),
        Fake("WebSearch", description="Search the web; fetch nothing."),
        Fake("NotebookEdit", description="Edit a Jupyter notebook cell."),
        Fake("mcp__slack__send_message", description="Post to a channel."),
        Fake("mcp__slack__list", description="List channels."),
        Fake("mcp__mail__send", description="Send an email to a slack-like inbox."),
    ]})
    # A query that is a tool's name loads that tool alone.
    assert search.search("webfetch") == ["WebFetch"]
    # An mcp__ prefix loads that server's tools.
    assert search.search("mcp__slack") == ["mcp__slack__send_message", "mcp__slack__list"]
    # +word filters on the name and, as a whole word, the description.
    assert search.search("+slack send") == [
        "mcp__slack__send_message", "mcp__mail__send", "mcp__slack__list"]
    assert search.search("+inbox send") == ["mcp__mail__send"]
    assert search.search("+inbo send") == []
    # Name words outrank description words.
    assert search.search("web fetch", max_results=2) == ["WebFetch", "WebSearch"]
    assert search.search("jupyter", max_results=0) == []
    schema = search.input_schema
    assert schema["required"] == ["query", "max_results"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_results"]["default"] == 5


def test_the_whole_name_counts_for_each_term_on_its_own() -> None:
    search = ToolSearchTool({t.name: t for t in [
        Fake("mcp__slack__list", description="List channels."),
        Fake("mcp__slack__send_message", description="Post to a channel."),
    ]})
    # "slack" scores both alike; "ack__send" is only in one tool's whole
    # name, which counts though "slack" already scored.
    assert search.search("slack ack__send") == ["mcp__slack__send_message",
                                                "mcp__slack__list"]


@pytest.mark.parametrize("limit", [float("inf"), float("-inf"), float("nan"), "5", None])
async def test_an_unusable_max_results_means_the_default(limit: Any) -> None:
    # JSON's 1e400 arrives as float("inf"), which int() cannot take.
    search = ToolSearchTool({f"Tool{n}": Fake(f"Tool{n}") for n in range(7)})
    ctx = ToolContext(task_id="t", call_id="c", env=None, state={}, record=lambda *_: None)
    out = await search.run({"query": "tool", "max_results": limit}, ctx)
    assert not out.is_error
    assert len(out.content) == 5


def test_select_takes_an_mcp_tool_by_its_own_name() -> None:
    search = ToolSearchTool({t.name: t for t in [
        Fake("mcp__slack__send"), Fake("mcp__mail__send"), Fake("mcp__docs__query-docs")]})
    assert search.search("select:query-docs") == ["mcp__docs__query-docs"]
    assert search.search("select:send") == ["mcp__slack__send", "mcp__mail__send"]
    many = ",".join(["nope"] * 64 + ["query-docs"])
    assert search.search(f"select:{many}") == []  # only the first 64 names count


class Offers(Extension):
    name = "offers"

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = list(tools)

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return self._tools


class CompactOnce(Extension):
    name = "compact"

    def __init__(self) -> None:
        self.done = False

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if not self.done and loaded_tools(view.state):
            self.done = True
            view.compacted(Compacted(summary="(summary)", through=view.state.last_seq))
        return request


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_a_loaded_tool_is_sent_and_callable_and_survives_compaction(
        store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("WebFetch", {})),
        reply("", ("ToolSearch", {"query": "select:WebFetch", "max_results": 1})),
        reply("", ("WebFetch", {})),
        reply("done"),
    ])
    ext = Offers([Fake("Read"), Fake("WebFetch", deferred=True)])
    agent = Agent(name="a", model="m", extensions=[ext, ToolSearch(), CompactOnce()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    first, _, after_load, after_compact = provider.requests
    assert names(first.tools) == ["Read", PLACEHOLDER + "*", "ToolSearch"]
    assert names(after_load.tools) == ["Read", "ToolSearch", "WebFetch*", PLACEHOLDER + "*"]
    assert names(after_compact.tools) == names(after_load.tools)
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    early, loaded, ran = results
    assert early.is_error and "select:WebFetch" in early.text
    assert loaded.content == [ToolReference(tool_name="WebFetch")]
    assert ran.text == "WebFetch ran"
    assert TaskView(state=TaskState(), extensions=[ext]).deferred_tool_names() == ["WebFetch"]


async def test_a_continued_task_keeps_the_tools_its_source_loaded(store: SqliteStore) -> None:
    provider = ScriptedProvider([
        reply("", ("ToolSearch", {"query": "select:WebFetch", "max_results": 1})),
        reply("loaded"),
        reply("", ("WebFetch", {})),
        reply("done"),
    ])
    ext = Offers([Fake("Read"), Fake("WebFetch", deferred=True)])
    agent = Agent(name="a", model="m", extensions=[ext, ToolSearch()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None, sleep=_no_sleep)  # type: ignore[arg-type,return-value]
    client = Client(store)
    src = await client.create("a", "load it")
    await rt.run_until_idle()
    resumed = await client.create("a", "now use it", history_from=src)
    await rt.run_until_idle()
    first = provider.requests[2]
    assert "WebFetch*" in names(first.tools)
    assert any(isinstance(b, ToolReference) for m in first.messages for r in m.content
               if isinstance(r, ToolResult) for b in r.content)
    [ran] = [r.event.result for r in await client.events(resumed)
             if isinstance(r.event, ToolFinished)]
    assert not ran.is_error and ran.text == "WebFetch ran"


def _result_message() -> Message:
    return Message(role="user", content=[
        ToolResult(call_id="c1", content=[ToolReference(tool_name="WebFetch")]),
        Text(text="Tool loaded.")])


def test_anthropic_sends_tool_references_and_defer_loading() -> None:
    request = ModelRequest(model="claude-x", max_tokens=100, messages=[_result_message()], tools=[
        ToolSpec(name="WebFetch", description="d", input_schema={}, defer_loading=True),
        ToolSpec(name="Read", description="d", input_schema={})])
    body = AnthropicProvider(api_key="k").build_body(request)
    assert body["tools"][0]["defer_loading"] is True and "defer_loading" not in body["tools"][1]
    result, loaded = body["messages"][0]["content"]
    assert result["content"] == [{"type": "tool_reference", "tool_name": "WebFetch"}]
    assert loaded == {"type": "text", "text": "Tool loaded."}


def test_other_wires_name_the_loaded_tools_as_text() -> None:
    request = ModelRequest(model="gpt-x", messages=[_result_message()])
    body = OpenAICompatProvider(api_key="k", base_url="http://x").build_body(request)
    assert any(m.get("content") == "Loaded: WebFetch" for m in body["messages"])
