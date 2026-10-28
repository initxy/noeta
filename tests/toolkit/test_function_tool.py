"""``function_tool``: a typed function becomes a tool the model can call."""

from __future__ import annotations

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
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.text for r in results] == [
        "5", "add: missing a", '{"kind": "task", "id": "t1", "tags": []}']
    assert [r.is_error for r in results] == [False, True, False]
    await store.close()
