"""``tool`` — turn a plain function into a Tool + matching ToolRef.

A library author writes a function ``fn(arguments, ctx) -> ToolResult`` and
wraps it with :func:`tool`, getting back a single object that is **both** a
runnable :class:`noeta.protocols.tool.Tool` (it has ``name`` / ``risk_level`` /
``input_schema`` and an ``invoke`` method) **and** a carrier of the matching
:class:`noeta.agent.spec.ToolRef` under ``.ref``. The ref is the identity an
:class:`~noeta.agent.spec.AgentSpec` references by value; keeping the runnable
and the ref on one object means a recipe can wire ``spec.tools`` and the live
tool from the same definition without the metadata fields drifting apart.

Why ``version`` is required (no default): the ref's ``(name, version,
risk_level)`` tuple is the tool's declared identity inside an ``AgentSpec``. A
silent default version would let two behaviourally different tools share an
identity, so we force the author to state it.

``input_schema`` is passed explicitly as a hand-written JSON-Schema-shaped dict.
This repo authors those dicts by hand (see ``noeta.tools.fs`` / ``noeta.tools.mcp``)
rather than deriving them from type hints; the decorator stays in step and adds
no schema-derivation dependency.

Layer note: this module lives in the ``noeta.tools`` band, which sits above
``noeta.agent`` and ``noeta.protocols`` in the layering — so importing
``ToolRef`` (identity) and the ``Tool`` Protocol is allowed. It never reaches up
into ``noeta.agent``.
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

    Satisfies the :class:`noeta.protocols.tool.Tool` Protocol structurally — it
    exposes the metadata attributes plus :meth:`invoke` — while also publishing
    ``.ref`` so the same object can be dropped into an
    :class:`~noeta.agent.spec.AgentSpec`'s ``tools``. The ref fields are the very
    same values used for the Tool metadata, so the runnable and the ref can
    never disagree.
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
        """The :class:`ToolRef` an ``AgentSpec`` references this tool by.

        Built from the same fields the Tool metadata exposes, so ``spec.tools``
        and the live tool stay identical by construction.
        """
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

    ``version`` is **required**; omitting it raises ``TypeError`` (a default
    would let unrelated tools collide on identity inside an ``AgentSpec``).
    ``input_schema`` is a hand-written JSON-Schema-shaped dict — LLM-facing
    metadata that Noeta does not validate ``arguments`` against at runtime.
    ``description`` is the hand-written, LLM-facing statement of what the tool
    does — the model's single source of tool semantics, rendered
    into the provider tool schema; library authors should supply one.

    Returns a :class:`DecoratedTool` when ``fn`` is supplied directly, or a
    decorator awaiting the function when used as ``@tool(...)``.
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
