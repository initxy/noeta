"""The ``@tool`` decorator/factory from ``noeta.tools``.

A decorated function has to serve as the runnable Tool AND as the ``AgentSpec``
entry that names it, so the ``.ref`` it publishes must equal a hand-written
``ToolRef`` field for field — otherwise a spec and the tool it declares drift
apart silently. ``version`` has no default for the same reason: an unversioned
ref cannot pin what the agent was built against.
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
    assert (ref.name, ref.version, ref.risk_level) == (
        t.name,
        t.version,
        t.risk_level,
    )
    # Value equality with a hand-written ref is the point: a spec author may
    # write either form and must get the same identity.
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
