"""``tool`` — wrap a plain function as a runnable Tool carrying its ToolRef.

The wrapper is deliberately one object rather than two: an author wires
``spec.tools`` and the live tool from the same definition, so the ref's
identity fields can never drift from the Tool metadata they were built from.
``input_schema`` stays a hand-written JSON-Schema-shaped dict — deriving it from
type hints would pull a schema-derivation dependency into the kernel.
"""

from __future__ import annotations

from typing import Any, Callable

from noeta.agent.spec import ToolRef
from noeta.protocols.tool import ToolContext, ToolResult


__all__ = ["DecoratedTool", "tool"]


#: A bare tool function: ``fn(arguments, ctx) -> ToolResult``.
ToolFn = Callable[[dict[str, Any], ToolContext], ToolResult]


class DecoratedTool:
    """A function wrapped as a Tool, carrying its matching :class:`ToolRef`.

    Satisfies the :class:`noeta.protocols.tool.Tool` Protocol structurally while
    also publishing ``.ref``, so the same object can be dropped into an
    ``AgentSpec``'s ``tools``. The ref is built from the very fields the Tool
    metadata exposes, so the runnable and the ref cannot disagree.
    """

    __slots__ = (
        "_fn",
        "name",
        "version",
        "risk_level",
        "input_schema",
        "description",
    )

    def __init__(
        self,
        fn: ToolFn,
        *,
        name: str,
        version: str,
        risk_level: str,
        input_schema: dict[str, Any],
        description: str = "",
    ) -> None:
        self._fn = fn
        self.name = name
        self.version = version
        self.risk_level = risk_level
        self.input_schema = input_schema
        self.description = description

    @property
    def ref(self) -> ToolRef:
        """The :class:`ToolRef` an ``AgentSpec`` references this tool by."""
        return ToolRef(
            name=self.name,
            version=self.version,
            risk_level=self.risk_level,
        )

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        return self._fn(arguments, ctx)


def tool(
    fn: ToolFn | None = None,
    *,
    name: str,
    version: str | None = None,
    risk_level: str = "low",
    input_schema: dict[str, Any],
    description: str = "",
) -> DecoratedTool | Callable[[ToolFn], DecoratedTool]:
    """Wrap ``fn(arguments, ctx) -> ToolResult`` as a Tool with a matching ref.

    Two call forms:

        my_tool = tool(read_file, name="read", version="1", input_schema=SCHEMA)

        @tool(name="read", version="1", input_schema=SCHEMA)
        def read(arguments, ctx): ...

    ``version`` is **required**: ``(name, version, risk_level)`` is the tool's
    declared identity inside an ``AgentSpec``, and a silent default would let two
    behaviourally different tools share one. ``input_schema`` is LLM-facing
    metadata only — ``arguments`` is never validated against it at runtime.
    ``description`` is the model's single source of tool semantics, rendered into
    the provider tool schema, so authors should supply one.
    """
    if version is None:
        raise TypeError(
            "tool() requires a 'version' (no default) — the version feeds the "
            "AgentSpec fingerprint, so it must be stated explicitly."
        )

    def wrap(target: ToolFn) -> DecoratedTool:
        return DecoratedTool(
            target,
            name=name,
            version=version,
            risk_level=risk_level,
            input_schema=input_schema,
            description=description,
        )

    if fn is None:
        return wrap
    return wrap(fn)
