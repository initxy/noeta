"""``noeta.tools.tool`` — the @tool decorator/factory.

Behaviours under test:
    * a wrapped function is a runnable Tool: it carries the four metadata
      fields and ``invoke`` delegates to the function
    * it also publishes a ``.ref`` ToolRef whose four fields match the Tool
      metadata, and which ``==`` a hand-written ToolRef
    * ``version`` is required — omitting it raises a clear error
    * both call forms (direct factory and ``@tool(...)`` decorator) work
"""

from __future__ import annotations

import pytest

from noeta.agent.spec import ToolRef
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.storage.memory import InMemoryContentStore
from noeta.tools import DecoratedTool, tool


SCHEMA = {
    "type": "object",
    "properties": {"msg": {"type": "string"}},
    "additionalProperties": False,
}


def _echo(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=arguments.get("msg"), summary="ok")


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def test_factory_produces_runnable_tool_with_metadata() -> None:
    t = tool(
        _echo,
        name="echo",
        version="3",
        risk_level="medium",
        input_schema=SCHEMA,
    )

    assert isinstance(t, DecoratedTool)
    assert t.name == "echo"
    assert t.risk_level == "medium"
    assert t.input_schema == SCHEMA

    result = t.invoke({"msg": "hi"}, _ctx())
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == "hi"


def test_ref_matches_metadata_and_equals_handwritten_ref() -> None:
    t = tool(
        _echo,
        name="echo",
        version="3",
        risk_level="medium",
        input_schema=SCHEMA,
    )

    ref = t.ref
    assert isinstance(ref, ToolRef)
    # The three ref fields mirror the Tool metadata exactly.
    assert (ref.name, ref.version, ref.risk_level) == (
        t.name,
        t.version,
        t.risk_level,
    )
    # And equals a ref written out by hand (value equality on the dataclass).
    assert ref == ToolRef(
        name="echo", version="3", risk_level="medium"
    )


def test_defaults_for_risk_level() -> None:
    t = tool(_echo, name="echo", version="1", input_schema=SCHEMA)

    assert t.risk_level == "low"
    assert t.ref == ToolRef(name="echo", version="1")


def test_decorator_form_wraps_function() -> None:
    @tool(name="echo", version="2", input_schema=SCHEMA)
    def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(success=True, output=arguments.get("msg"))

    assert isinstance(echo, DecoratedTool)
    assert echo.name == "echo"
    assert echo.ref == ToolRef(name="echo", version="2")
    assert echo.invoke({"msg": "yo"}, _ctx()).output == "yo"


def test_missing_version_raises() -> None:
    with pytest.raises((TypeError, ValueError)):
        tool(_echo, name="echo", input_schema=SCHEMA)


def test_missing_version_raises_in_decorator_form() -> None:
    with pytest.raises((TypeError, ValueError)):

        @tool(name="echo", input_schema=SCHEMA)
        def echo(arguments: dict, ctx: ToolContext) -> ToolResult:
            return ToolResult(success=True)
