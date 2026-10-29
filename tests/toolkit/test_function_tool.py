"""``function_tool``: a typed function becomes a tool the model can call."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated, Literal

import pytest

from noeta import Agent, Runtime, ToolContext, function_tool
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.extension import ToolSet
from noeta.testing import ScriptedProvider, reply


@function_tool(read_only=True)
def add(a: int, b: Annotated[int, "added to a"] = 1) -> str:
    """Add two whole numbers."""
    return str(a + b)


@function_tool
async def describe(kind: Literal["task", "call"], tags: list[str] | None,
                   ctx: ToolContext) -> dict[str, object]:
    """Say which task or call this is."""
    return {"kind": kind, "id": ctx.task_id if kind == "task" else ctx.call_id,
            "tags": tags or []}


def test_the_schema_comes_from_the_annotations() -> None:
    assert add.name == "add" and add.description == "Add two whole numbers."
    assert add.read_only and not add.clearable
    assert add.input_schema == {
        "type": "object", "additionalProperties": False, "required": ["a"],
        "properties": {"a": {"type": "integer"},
                       "b": {"type": "integer", "description": "added to a"}}}
    assert describe.input_schema["properties"] == {
        "kind": {"enum": ["task", "call"]},
        "tags": {"type": "array", "items": {"type": "string"}}}
    assert describe.input_schema["required"] == ["kind", "tags"]


def test_a_function_needs_a_docstring_and_known_annotations() -> None:
    def bare(x: int) -> str:
        return ""

    with pytest.raises(ValueError, match="docstring"):
        function_tool(bare)
    assert function_tool(description="d", name="other")(bare).name == "other"

    def odd(x: complex) -> str:
        """Odd."""
        return ""

    with pytest.raises(TypeError, match="complex"):
        function_tool(odd)


async def test_the_model_calls_it(tmp_path: Path) -> None:
    agent = Agent(name="a", model="m", extensions=(ToolSet([add, describe]),))
    provider = ScriptedProvider([
        reply("", ("add", {"a": 2, "b": 3}), ("add", {"b": 1}),
              ("describe", {"kind": "task", "tags": None})),
        reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    client = Client(store)
    tid = await client.create("a", "go", task_id="t1")
    await rt.run_until_idle()
    results = sorted((r.event.result for r in await client.events(tid)
                      if isinstance(r.event, ToolFinished)),
                     key=lambda r: int(r.call_id.rsplit("_", 1)[1]))
    assert [r.text for r in results] == [
        "5", "add: missing a", '{"kind": "task", "id": "t1", "tags": []}']
    assert [r.is_error for r in results] == [False, True, False]
    await store.close()


async def test_a_sync_function_runs_off_the_event_loop() -> None:
    @function_tool
    def where() -> str:
        """Name the thread."""
        return threading.current_thread().name

    out = await where.run({}, ToolContext(task_id="t", call_id="c", env=None, state={},
                                       record=lambda *_: None))
    assert out.content[0].text != threading.current_thread().name  # type: ignore[union-attr]


async def test_star_parameters_stay_out_of_the_schema() -> None:
    @function_tool
    def greet(name: str, *args: str, **kwargs: str) -> str:
        """Greet someone."""
        return f"hi {name}"

    assert greet.input_schema == {"type": "object", "additionalProperties": False,
                                  "required": ["name"],
                                  "properties": {"name": {"type": "string"}}}
    out = await greet.run({"name": "a"}, ToolContext(task_id="t", call_id="c", env=None,
                                                     state={}, record=lambda *_: None))
    assert not out.is_error


async def test_an_optional_context_and_positional_only_parameters() -> None:
    @function_tool
    def scale(x: int, factor: int = 2, /, *, ctx: ToolContext | None = None) -> str:
        """Scale a number."""
        return f"{x * factor} in {ctx.task_id if ctx else None}"

    assert scale.input_schema["properties"] == {"x": {"type": "integer"},
                                                "factor": {"type": "integer"}}
    ctx = ToolContext(task_id="t", call_id="c", env=None, state={}, record=lambda *_: None)
    out = await scale.run({"x": 3}, ctx)
    assert out.content[0].text == "6 in t"  # type: ignore[union-attr]
    out = await scale.run({"x": 3, "factor": 5}, ctx)
    assert out.content[0].text == "15 in t"  # type: ignore[union-attr]
