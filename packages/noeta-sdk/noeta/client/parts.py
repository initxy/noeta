"""SDK built-in parts table.

This module is the **single source** for the built-in tool-name → class
mapping and the policy/composer ComponentRefs (roster removed; there is no
``noeta.agent.roster.specs`` mirror). The SDK owns these identity constants;
the product side consumes the compiled output via :mod:`noeta.client.options`
+ :mod:`noeta.presets`, with no second copy.

Keeping the table in ``noeta.client`` (rather than importing from ``noeta.agent``)
makes ``noeta-sdk`` self-contained so library users do not pull in the coding-agent
product package.
"""

from __future__ import annotations

import importlib
from dataclasses import MISSING, fields
from typing import Any, Callable, Optional

from noeta.agent.spec import ComponentRef, ToolRef


__all__ = [
    "BUILTIN_TOOL_CLASSES",
    "COMPOSER_REF",
    "POLICY_REF",
    "builtin_tool_classes",
    "builtin_tool_ref",
    "default_tool_factories",
]


def _resolve_ref(ref: str) -> Any:
    """Resolve an explicit ``pkg.mod:attr`` manifest ref (microkernel M1).

    The same doorway the plugin loader uses — a dynamic import at the
    sanctioned execution boundary. SDK core keeps **no static edge** into the
    builtins band.
    """
    module_name, _, attr = ref.partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


_TOOL_CLASS_CACHE: Optional[dict[str, type]] = None


def builtin_tool_classes() -> dict[str, type]:
    """name → class for the default built-in tools, **loader-resolved**.

    Microkernel M1 (D2): SDK core holds no static tool table. The ``fs`` /
    ``web`` built-in plugin manifests declare the default 11 tools; this
    function reads those manifests (inert data) and resolves each ``tool``
    contribution's ref at the sanctioned execution boundary (client build /
    compile). Memoized — the classes are import-stable for the process.

    ``web_search`` is listed like every other name, but the *runtime pack*
    constructs it only when ``NOETA_WEB_SEARCH_API_KEY`` is set — the
    whitelist's intersection with the built pack drops it otherwise, so the
    model never sees a search tool it cannot use.
    """
    global _TOOL_CLASS_CACHE
    if _TOOL_CLASS_CACHE is None:
        builtins_mod = importlib.import_module("noeta.builtins")
        out: dict[str, type] = {}
        for manifest in builtins_mod.builtin_manifests():
            if manifest.name not in ("fs", "web"):
                continue
            for c in manifest.contributions:
                if c.surface != "tool" or c.ref is None:
                    continue
                out[c.name] = _resolve_ref(c.ref)
        _TOOL_CLASS_CACHE = out
    return _TOOL_CLASS_CACHE


def default_tool_factories() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """``(fs_tools_factory, web_tools_factory)`` for the kernel builder.

    Resolved from the ``fs`` / ``web`` built-in plugin bodies
    (``noeta.builtins.<name>.impl:build_*_tools``) — the injection the
    microkernel builder requires (its ``fs_tools_factory`` /
    ``web_tools_factory`` params); the kernel itself imports no tool
    implementation.
    """
    return (
        _resolve_ref("noeta.builtins.fs.impl:build_fs_tools"),
        _resolve_ref("noeta.builtins.web.impl:build_web_tools"),
    )


def __getattr__(name: str) -> Any:
    # Compatibility surface: ``BUILTIN_TOOL_CLASSES`` stays importable as a
    # module attribute, now backed by the loader-resolved table.
    if name == "BUILTIN_TOOL_CLASSES":
        return builtin_tool_classes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: ReAct decision-mapping behaviour version — SDK single source
#: (``roster.specs._REACT_POLICY`` removed).
POLICY_REF = ComponentRef("react", "1")
#: Three-segment context composer version — SDK single source
#: (``roster.specs._THREE_SEGMENT_COMPOSER`` removed).
COMPOSER_REF = ComponentRef("three_segment", "v3")


def _field_default(cls: type, field_name: str) -> Any:
    """Return the static dataclass-field default of ``cls.field_name``.

    SDK single implementation (``roster.specs._field_default`` removed).
    Raises ``TypeError`` if the field has no static default (callers would
    otherwise silently get a ``MISSING`` sentinel into the AgentSpec identity,
    which is the bug this guard prevents).
    """
    for f in fields(cls):
        if f.name == field_name:
            if f.default is MISSING:
                raise TypeError(
                    f"{cls.__name__}.{field_name} has no static default; "
                    f"cannot read tool identity metadata without instantiation"
                )
            return f.default
    raise AttributeError(f"{cls.__name__} has no field {field_name!r}")


def builtin_tool_ref(name: str) -> ToolRef:
    """Return a :class:`ToolRef` for the built-in tool ``name``.

    ``version`` is hard-coded to ``"1"`` (the SDK-wide convention for
    built-in tools; a bump in tool behaviour should add a new name or
    bump the component refs). ``risk_level`` is read straight off the tool
    class' static default, so a class-level change in risk surfaces in the
    compiled ``AgentSpec``.

    Raises
    ------
    KeyError
        If ``name`` is not in :data:`BUILTIN_TOOL_CLASSES`. The message
        enumerates the valid names.
    """
    classes = builtin_tool_classes()
    if name not in classes:
        available = ", ".join(sorted(classes))
        raise KeyError(
            f"Unknown built-in tool {name!r}. Available: {available}"
        )
    cls = classes[name]
    return ToolRef(
        name=name,
        version="1",
        risk_level=str(_field_default(cls, "risk_level")),
    )
