"""``FakeTool``: a scripted tool for driving ``tool_calls`` without real I/O.

The three behaviours every integration test built on it depends on: a small
output comes back inline, an output past the 4 KB payload ceiling is written
to the artifact store and surfaced as ``ToolResult.artifacts`` with ``output``
left empty, and an unscripted argument set returns a failed ``ToolResult``
rather than raising — so a Policy can react to it like any tool failure.
"""

from __future__ import annotations

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.fake import FakeTool


def _ctx(store: InMemoryContentStore) -> ToolContext:
    return ToolContext(artifact_store=store)


def test_fake_tool_returns_scripted_inline_output_for_small_payload() -> None:
    tool = FakeTool(
        name="echo",
        script={("hello",): "world"},
    )
    store = InMemoryContentStore()

    result = tool.invoke({"msg": "hello"}, _ctx(store))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == "world"
    assert result.artifacts == []


def test_fake_tool_writes_large_output_to_artifact_store() -> None:
    big = "x" * 5000  # > 4 KB → must go through ContentStore
    tool = FakeTool(
        name="echo",
        script={("big",): big},
    )
    store = InMemoryContentStore()

    result = tool.invoke({"msg": "big"}, _ctx(store))

    assert result.success is True
    # Inline output is dropped when we offload to artifacts.
    assert result.output in (None, "")
    assert len(result.artifacts) == 1
    artifact_ref = result.artifacts[0]
    assert artifact_ref.size == len(big.encode("utf-8"))
    assert store.get(artifact_ref).decode("utf-8") == big


def test_fake_tool_returns_failure_for_unscripted_arguments() -> None:
    tool = FakeTool(name="echo", script={("hi",): "ok"})

    result = tool.invoke({"msg": "unknown"}, _ctx(InMemoryContentStore()))

    assert result.success is False
