"""A plain typed Python function as a :class:`~noeta.core.tool.Tool`.

::

    @function_tool(read_only=True)
    def add(a: int, b: Annotated[int, "added to a"] = 1) -> str:
        \"\"\"Add two whole numbers.\"\"\"
        return str(a + b)

The tool's name is the function's (or ``name=``), its description the
docstring, and its input schema comes from the parameters' annotations:
``str``, ``int``, ``float``, ``bool``, ``list[...]``, ``dict``, ``Literal``
(an enum) and ``X | None``; ``Annotated[T, "text"]`` describes a parameter,
and a parameter with a default is optional. A parameter annotated
``ToolContext`` (or named ``ctx``) gets the call's context and is left out of
the schema. The function may be sync (it runs in a worker thread) or async
and returns ``str``, a ``ToolOutput``, or anything JSON can show.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import types
import typing
from collections.abc import Callable
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output

__all__ = ["function_tool"]

_SCALARS: dict[Any, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema(hint: Any) -> dict[str, Any]:
    """The JSON schema of one annotation."""
    origin, args = get_origin(hint), get_args(hint)
    if origin is Annotated:
        out = _schema(args[0])
        text = next((a for a in args[1:] if isinstance(a, str)), None)
        return {**out, "description": text} if text else out
    if origin in (Union, types.UnionType):
        rest = [a for a in args if a is not type(None)]
        return _schema(rest[0]) if len(rest) == 1 else {"anyOf": [_schema(a) for a in rest]}
    if origin is Literal:
        return {"enum": list(args)}
    if hint in _SCALARS:
        return {"type": _SCALARS[hint]}
    if hint is list or origin is list:
        return {"type": "array", **({"items": _schema(args[0])} if args else {})}
    if hint is dict or origin is dict:
        return {"type": "object"}
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    raise TypeError(f"function_tool: no JSON schema for the annotation {hint!r}")


def _is_context(param: inspect.Parameter, hint: Any) -> bool:
    return hint is ToolContext or (hint is inspect.Parameter.empty and param.name == "ctx")


class _FunctionTool:
    def __init__(self, fn: Callable[..., Any], *, name: str, description: str,
                 read_only: bool, clearable: bool, deferred: bool) -> None:
        self.fn = fn
        self.name = name
        self.description = description
        self.read_only = read_only
        self.clearable = clearable
        self.deferred = deferred
        hints = get_type_hints(fn, include_extras=True)
        self._context: str | None = None
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in inspect.signature(fn).parameters.values():
            hint = hints.get(param.name, inspect.Parameter.empty)
            if _is_context(param, hint):
                self._context = param.name
                continue
            properties[param.name] = _schema(hint)
            if param.default is inspect.Parameter.empty:
                required.append(param.name)
        self.input_schema: dict[str, Any] = {"type": "object", "properties": properties,
                                             "additionalProperties": False}
        if required:
            self.input_schema["required"] = required
        self._params = set(properties)
        self._required = required

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        missing = [p for p in self._required if p not in input]
        unknown = sorted(set(input) - self._params)
        if missing or unknown:
            problem = (f"missing {', '.join(missing)}" if missing
                       else f"unknown parameter(s) {', '.join(unknown)}")
            return text_output(f"{self.name}: {problem}", is_error=True)
        kwargs = dict(input)
        if self._context is not None:
            kwargs[self._context] = ctx
        if inspect.iscoroutinefunction(self.fn):
            result = await self.fn(**kwargs)
        else:
            # Off the event loop: a blocking function must not stall every
            # other task of the worker.
            result = await asyncio.to_thread(self.fn, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        if isinstance(result, ToolOutput):
            return result
        if isinstance(result, str):
            return text_output(result)
        return text_output(json.dumps(result, ensure_ascii=False, default=str))


@typing.overload
def function_tool(fn: Callable[..., Any], /) -> Tool: ...


@typing.overload
def function_tool(*, name: str | None = None, description: str | None = None,
                  read_only: bool = False, clearable: bool = False,
                  deferred: bool = False) -> Callable[[Callable[..., Any]], Tool]: ...


def function_tool(fn: Callable[..., Any] | None = None, /, *, name: str | None = None,
                  description: str | None = None, read_only: bool = False,
                  clearable: bool = False, deferred: bool = False) -> Any:
    """``fn`` as a tool (see the module docstring); used bare
    (``@function_tool``) or with options (``@function_tool(read_only=True)``).
    ``read_only``: only reads, so it may run alongside other read-only
    calls; ``clearable``: an old result may be dropped from the context;
    ``deferred``: loaded through ``ToolSearch``."""

    def make(f: Callable[..., Any]) -> Tool:
        text = description if description is not None else inspect.getdoc(f) or ""
        if not text:
            raise ValueError(f"function_tool: {f.__name__} needs a docstring or a description")
        return _FunctionTool(f, name=name or f.__name__, description=text,
                             read_only=read_only, clearable=clearable, deferred=deferred)

    return make(fn) if fn is not None else make
